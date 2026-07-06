"""Unit tests for the hardened XML parser (no QGIS/GDAL needed).

safexml.fromstring is a dependency-free, attack-resistant stand-in for
xml.etree.ElementTree.fromstring used by the WMS/WMTS backends.

Run from the repo root:  pytest
"""
import xml.etree.ElementTree as ET

import pytest

from basemap_tile_downloader import safexml


def _local(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def test_namespaced_tags_match_elementtree():
    xml = (b'<WMS_Capabilities xmlns="http://www.opengis.net/wms" version="1.3.0">'
           b'<Capability><Request><GetMap>'
           b'<Format>image/png</Format><Format>image/tiff</Format>'
           b'</GetMap></Request></Capability></WMS_Capabilities>')
    # Tag naming ("{uri}local") must be identical to ElementTree so the backends'
    # namespace-stripping keeps working.
    assert [e.tag for e in safexml.fromstring(xml).iter()] == \
           [e.tag for e in ET.fromstring(xml).iter()]


def test_extracts_text_and_attributes():
    xml = (b'<WMS_Capabilities xmlns="http://www.opengis.net/wms" version="1.3.0">'
           b'<GetMap><Format>image/png</Format></GetMap></WMS_Capabilities>')
    root = safexml.fromstring(xml)
    assert root.get("version") == "1.3.0"
    assert [f.text for f in root.iter() if _local(f.tag) == "Format"] == ["image/png"]


def test_accepts_str_input():
    root = safexml.fromstring("<a><b>x</b></a>")
    assert root.find("b").text == "x"


def test_wms_111_external_dtd_doctype_is_allowed():
    # WMS 1.1.1 capabilities legitimately carry a DOCTYPE referencing an external
    # DTD; it must still parse (and must NOT be fetched over the network).
    xml = (b'<!DOCTYPE WMT_MS_Capabilities SYSTEM '
           b'"http://schemas.opengis.net/wms/1.1.1/WMS_MS_Capabilities.dtd">'
           b'<WMT_MS_Capabilities version="1.1.1"><Format>image/png</Format>'
           b'</WMT_MS_Capabilities>')
    root = safexml.fromstring(xml)
    assert [f.text for f in root.iter() if _local(f.tag) == "Format"] == ["image/png"]


def test_billion_laughs_is_rejected():
    xml = (b'<!DOCTYPE lolz [<!ENTITY lol "lol">'
           b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>'
           b'<root>&lol2;</root>')
    with pytest.raises(ET.ParseError):
        safexml.fromstring(xml)


def test_external_entity_xxe_does_not_leak():
    xml = (b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
           b'<root>&xxe;</root>')
    # Either rejected outright, or parsed without the entity ever being resolved.
    try:
        root = safexml.fromstring(xml)
    except ET.ParseError:
        return
    assert "root:" not in (root.text or "")


def test_malformed_raises_parseerror():
    with pytest.raises(ET.ParseError):
        safexml.fromstring(b"<a><b></a>")
