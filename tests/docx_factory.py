"""Builds minimal .docx files for tests (raw OOXML, no python-docx needed)."""

import io
import zipfile

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
)
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

_CONTENT_TYPES = (
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)
_RELS = (
    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)


def para(text: str) -> str:
    return f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'


def table(*cells: str) -> str:
    return '<w:tbl><w:tr>' + ''.join(f'<w:tc>{c}</w:tc>' for c in cells) + '</w:tr></w:tbl>'


def textbox_run(content_xml: str) -> str:
    """A run holding a text box the way Word writes it: Choice + VML Fallback."""
    return (
        '<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:anchor><a:graphic><a:graphicData>'
        f'<wps:wsp><wps:txbx><w:txbxContent>{content_xml}</w:txbxContent></wps:txbx></wps:wsp>'
        '</a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>'
        f'<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>{content_xml}</w:txbxContent></v:textbox>'
        '</v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>'
    )


def build_docx(body_xml: str, extra_parts: dict | None = None) -> bytes:
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document {NS}><w:body>{body_xml}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', _CONTENT_TYPES)
        z.writestr('_rels/.rels', _RELS)
        z.writestr('word/document.xml', doc)
        for name, data in (extra_parts or {}).items():
            z.writestr(name, data)
    return buf.getvalue()
