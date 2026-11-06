"""Glossary, do-not-translate rules and glossary candidate review routes."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...services.lakebase import get_pool
from ...services.translation import glossary_lakebase as _glossary_lakebase
from ...services.translation.glossary_io import dnt_rule_problem
from ...services.translation.glossary_lakebase import generate_term_id
from ...services.user import get_user_identity, require_translate

router = APIRouter()


class GlossaryTermIn(BaseModel):
    term_id: Optional[str] = None  # omitted/blank -> server generates T####
    en: str = ''
    fr: str = ''
    cs: str = ''
    bg: str = ''
    de: str = ''
    es: str = ''
    pt: str = ''
    ar: str = ''
    domain: str = ''
    notes: str = ''
    definition: str = ''
    # 'REF#chunk' (extracted from a terminology section), 'generated' (LLM), 'cross_lang_pair' (authentic
    # definition corroborated by a confirmed translation pair); empty without a definition.
    definition_source: str = ''


class DntRuleIn(BaseModel):
    pattern: str
    type: str = ''
    match_mode: str = 'exact'
    notes: str = ''


@router.get('/translate/glossary', dependencies=[Depends(require_translate)])
async def list_glossary() -> Any:
    """List every glossary term and DNT rule.

    :return: {'terms', 'dnt_rules', 'available'}; available is False without a database.
    """
    pool = get_pool()
    if not pool:
        return {'terms': [], 'dnt_rules': [], 'available': False}
    async with pool.acquire() as conn:
        term_rows = await conn.fetch(
            'SELECT term_id, en, fr, cs, bg, de, es, pt, ar, domain, notes, '
            'definition, definition_source FROM glossary_terms ORDER BY term_id'
        )
        dnt_rows = await conn.fetch(
            'SELECT id, pattern, type, match_mode, notes FROM dnt_rules ORDER BY id'
        )
    return {
        'terms': [dict(r) for r in term_rows],
        'dnt_rules': [dict(r) for r in dnt_rows],
        'available': True,
    }


@router.post('/translate/glossary/terms', dependencies=[Depends(require_translate)])
async def upsert_glossary_term(body: GlossaryTermIn) -> Any:
    """Create or update a glossary term.

    :param body: term fields; a blank term_id makes the server generate the next one.
    :return: {'term_id': id}, or a JSONResponse with 503 without a database.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)

    term_id = (body.term_id or '').strip()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not term_id:
                term_id = await generate_term_id(conn)

            await conn.execute(
                '''
                INSERT INTO glossary_terms (term_id, en, fr, cs, bg, de, es, pt, ar, domain, notes,
                                            definition, definition_source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (term_id) DO UPDATE SET
                    en = EXCLUDED.en, fr = EXCLUDED.fr, cs = EXCLUDED.cs, bg = EXCLUDED.bg,
                    de = EXCLUDED.de, es = EXCLUDED.es, pt = EXCLUDED.pt, ar = EXCLUDED.ar,
                    domain = EXCLUDED.domain,
                    notes = EXCLUDED.notes, definition = EXCLUDED.definition,
                    definition_source = EXCLUDED.definition_source, updated_at = NOW()
                ''',
                term_id, body.en, body.fr, body.cs, body.bg, body.de, body.es, body.pt, body.ar,
                body.domain, body.notes, body.definition, body.definition_source,
            )
    return {'term_id': term_id}


@router.delete('/translate/glossary/terms/{term_id}', dependencies=[Depends(require_translate)])
async def delete_glossary_term(term_id: str) -> Any:
    """Delete a glossary term.

    :param term_id: term identifier.
    :return: {'deleted': term_id}, or a JSONResponse with 503 without a database.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM glossary_terms WHERE term_id = $1', term_id)
    return {'deleted': term_id}


@router.post('/translate/glossary/dnt', dependencies=[Depends(require_translate)])
async def add_dnt_rule(body: DntRuleIn) -> Any:
    """Add a do-not-translate rule.

    :param body: pattern, type, match mode and notes.
    :return: {'id': rule_id}, or a JSONResponse with 422 for a dangerous rule and 503 without a database.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    problem = dnt_rule_problem(body.pattern, body.match_mode)
    if problem:
        return JSONResponse({'error': problem}, status_code=422)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'INSERT INTO dnt_rules (pattern, type, match_mode, notes) VALUES ($1, $2, $3, $4) RETURNING id',
            body.pattern, body.type, body.match_mode, body.notes,
        )
    return {'id': row['id']}


@router.delete('/translate/glossary/dnt/{rule_id}', dependencies=[Depends(require_translate)])
async def delete_dnt_rule(rule_id: int) -> Any:
    """Delete a do-not-translate rule.

    :param rule_id: rule identifier.
    :return: {'deleted': rule_id}, or a JSONResponse with 503 without a database.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM dnt_rules WHERE id = $1', rule_id)
    return {'deleted': rule_id}


class GlossaryCandidateApprove(BaseModel):
    # Optional overrides — the reviewer can correct a language before
    # validating; blank fields fall back to the candidate row's own value.
    en: Optional[str] = None
    fr: Optional[str] = None
    cs: Optional[str] = None
    bg: Optional[str] = None
    de: Optional[str] = None
    es: Optional[str] = None
    pt: Optional[str] = None
    ar: Optional[str] = None
    domain: str = ''
    notes: str = ''


class GlossaryCandidateReject(BaseModel):
    reason: str = ''


@router.get('/translate/glossary/candidates', dependencies=[Depends(require_translate)])
async def list_glossary_candidates(status: str = 'pending', limit: int = 50, offset: int = 0) -> Any:
    """List glossary candidates of one review status, best-ranked first.

    :param status: 'pending', 'approved' or 'rejected'.
    :param limit: page size, clamped to 1..200.
    :param offset: rows to skip.
    :return: {'candidates', 'total', 'available'}; available is False without a database.
    """
    pool = get_pool()
    if not pool:
        return {'candidates': [], 'total': 0, 'available': False}
    limit = max(1, min(limit, 200))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''
            SELECT id, en, fr, cs, bg, de, es, pt, ar, n_docs, sources, definition,
                   definition_source, priority, status, reviewed_by, reviewed_at,
                   reject_reason
            FROM glossary_candidates
            WHERE status = $1
            ORDER BY priority ASC NULLS LAST, n_docs DESC, id ASC
            LIMIT $2 OFFSET $3
            ''',
            status, limit, offset,
        )
        total = await conn.fetchval(
            'SELECT COUNT(*) FROM glossary_candidates WHERE status = $1', status
        )
    return {'candidates': [dict(r) for r in rows], 'total': total, 'available': True}


@router.post('/translate/glossary/candidates/{candidate_id}/approve', dependencies=[Depends(require_translate)])
async def approve_glossary_candidate(candidate_id: int, body: GlossaryCandidateApprove, request: Request) -> Any:
    """Approve a pending candidate and add it to the glossary.

    :param candidate_id: glossary_candidates.id.
    :param body: optional per-language overrides, domain and notes; unset languages keep the candidate's value.
    :param request: incoming request; its identity scopes the job to its owner.
    :return: {'term_id': new term id}, or a JSONResponse with 404, 409 when already reviewed, or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    reviewer = identity['email'] or identity['user_id']

    async with pool.acquire() as conn:
        cand = await conn.fetchrow('SELECT * FROM glossary_candidates WHERE id = $1', candidate_id)
        if not cand:
            return JSONResponse({'error': 'Candidate not found'}, status_code=404)

        overrides = {'en': body.en, 'fr': body.fr, 'cs': body.cs, 'bg': body.bg, 'de': body.de, 'es': body.es,
                     'pt': body.pt, 'ar': body.ar}
        langs = {lang: (v if v is not None else cand[lang]) for lang, v in overrides.items()}

        async with conn.transaction():
            # Claim first so a double-click or a second reviewer cannot insert the term twice.
            claimed = await conn.execute(
                '''
                UPDATE glossary_candidates
                SET status = 'approved', reviewed_by = $2, reviewed_at = NOW()
                WHERE id = $1 AND status = 'pending'
                ''',
                candidate_id, reviewer,
            )
            if claimed == 'UPDATE 0':
                return JSONResponse({'error': 'This candidate was already reviewed'}, status_code=409)
            term_id = await generate_term_id(conn)
            await conn.execute(
                '''
                INSERT INTO glossary_terms (term_id, en, fr, cs, bg, de, es, pt, ar, domain, notes,
                                            definition, definition_source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ''',
                term_id, langs['en'], langs['fr'], langs['cs'], langs['bg'], langs['de'], langs['es'],
                langs['pt'], langs['ar'], body.domain, body.notes, cand['definition'], cand['definition_source'],
            )
    return {'term_id': term_id}


@router.post('/translate/glossary/candidates/approve-all', dependencies=[Depends(require_translate)])
async def approve_all_glossary_candidates(request: Request) -> Any:
    """Approve every pending candidate as is.

    :param request: incoming request, used to identify the reviewer.
    :return: {'approved': count}, or a JSONResponse with 503 without a database.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    reviewer = identity['email'] or identity['user_id']

    async with pool.acquire() as conn:
        async with conn.transaction():
            pending = await conn.fetch("SELECT * FROM glossary_candidates WHERE status = 'pending' FOR UPDATE")
            # Single lock + MAX() read for the whole batch, then a local
            # counter — same allocation the shared generate_term_id() does
            # per-call, inlined here to avoid one advisory-lock round trip
            # per candidate.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", _glossary_lakebase.TERM_ID_LOCK_KEY)
            max_id = await conn.fetchval(
                r"SELECT COALESCE(MAX(SUBSTRING(term_id FROM 2)::int), 0) FROM glossary_terms WHERE term_id ~ '^T\d+$'"
            )
            next_id = (max_id or 0) + 1
            for cand in pending:
                term_id = f'T{next_id:04d}'
                next_id += 1
                await conn.execute(
                    '''
                    INSERT INTO glossary_terms (term_id, en, fr, cs, bg, de, es, pt, ar, domain, notes,
                                                definition, definition_source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, '', '', $10, $11)
                    ''',
                    term_id, cand['en'], cand['fr'], cand['cs'], cand['bg'], cand['de'], cand['es'],
                    cand['pt'], cand['ar'], cand['definition'], cand['definition_source'],
                )
                await conn.execute(
                    '''
                    UPDATE glossary_candidates
                    SET status = 'approved', reviewed_by = $2, reviewed_at = NOW()
                    WHERE id = $1
                    ''',
                    cand['id'], reviewer,
                )
    return {'approved': len(pending)}


@router.post('/translate/glossary/candidates/{candidate_id}/reject', dependencies=[Depends(require_translate)])
async def reject_glossary_candidate(candidate_id: int, body: GlossaryCandidateReject, request: Request) -> Any:
    """Reject a pending candidate.

    :param candidate_id: glossary_candidates.id.
    :param body: optional rejection reason.
    :param request: incoming request, used to identify the reviewer.
    :return: {'rejected': candidate_id}, or a JSONResponse with 404 when missing or already reviewed, or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    reviewer = identity['email'] or identity['user_id']

    async with pool.acquire() as conn:
        result = await conn.execute(
            '''
            UPDATE glossary_candidates
            SET status = 'rejected', reviewed_by = $2, reviewed_at = NOW(), reject_reason = $3
            WHERE id = $1 AND status = 'pending'
            ''',
            candidate_id, reviewer, body.reason,
        )
    if result == 'UPDATE 0':
        return JSONResponse({'error': 'Candidate not found or already reviewed'}, status_code=404)
    return {'rejected': candidate_id}
