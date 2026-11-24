from typing import Union

from lxml import etree
from lxml.etree import ElementTree


def load_xml(xml: Union[str, bytes]) -> ElementTree:
    """Safely parse XML data to an ElementTree, without namespaces in tags.

    The strip uses string operations, not etree.QName: on a 20 MB manifest the QName walk costs several times the parse.
    """
    if not isinstance(xml, bytes):
        xml = xml.encode("utf8")
    root = etree.fromstring(xml)
    for elem in root.iter(etree.Element):
        tag = elem.tag
        if tag[0] == "{":
            elem.tag = tag.partition("}")[2]
        attrib = elem.attrib
        for name in [name for name in attrib if name[0] == "{"]:
            attrib[name.partition("}")[2]] = attrib.pop(name)
    etree.cleanup_namespaces(root)
    return root
