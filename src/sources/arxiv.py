from urllib.parse import urlencode
import xml.etree.ElementTree as ET

from js import fetch


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ATOM_NAMESPACE = {"atom": "http://www.w3.org/2005/Atom"}


def _text(entry, path):
    element = entry.find(path, ATOM_NAMESPACE)
    return "" if element is None or element.text is None else " ".join(element.text.split())


def _first_author(entry):
    author = entry.find("atom:author/atom:name", ATOM_NAMESPACE)
    return "" if author is None or author.text is None else author.text.strip()


def _pdf_url(entry):
    for link in entry.findall("atom:link", ATOM_NAMESPACE):
        if link.attrib.get("title") == "pdf":
            return link.attrib.get("href", "")
    return ""


def _parse_arxiv_feed(xml_text):
    root = ET.fromstring(xml_text)
    papers = []

    for entry in root.findall("atom:entry", ATOM_NAMESPACE):
        arxiv_url = _text(entry, "atom:id")
        title = _text(entry, "atom:title")
        abstract = _text(entry, "atom:summary")

        papers.append({
            "title": title,
            "url": arxiv_url,
            "pdf_url": _pdf_url(entry),
            "source": "arXiv",
            "type": "paper",
            "published_at": _text(entry, "atom:published"),
            "updated_at": _text(entry, "atom:updated"),
            "authors": [_first_author(entry)] if _first_author(entry) else [],
            "summary_input": abstract,
        })

    return papers


async def fetch_arxiv_papers(limit=10):
    query = urlencode({
        "search_query": "cat:cs.AI OR cat:cs.CL OR cat:cs.LG OR cat:stat.ML",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": limit,
    })
    response = await fetch(f"{ARXIV_API_URL}?{query}")

    if not response.ok:
        raise RuntimeError(f"arXiv request failed with status {response.status}")

    xml_text = await response.text()
    return _parse_arxiv_feed(xml_text)
