"""Read-only ESCO catalog lookup for manual taxonomy review."""

import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from backend.matching_extraction import normalize_text

ESCO_VERSION = "1.2.0"
SOURCE_URI = "https://esco.ec.europa.eu/en/use-esco/download"
LICENSE = "Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)"
RDF_MEMBER = "esco-v1.2.0.rdf"
NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "status": "http://purl.org/iso25964/skos-thes#",
}
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
RDF_ABOUT = "{" + NS["rdf"] + "}about"


def _parse_concept(element):
    uri = element.get(RDF_ABOUT, "")
    if "/skill/" not in uri or any(
            child.tag == "{" + NS["status"] + "}status" and
            (child.text or "").strip().casefold() == "obsolete" for child in element):
        return None
    preferred = sorted({(child.text or "").strip() for child in element.findall("skos:prefLabel", NS)
                        if child.get(XML_LANG) == "en" and (child.text or "").strip()}, key=str.casefold)
    if not preferred:
        return None
    alternates = sorted({(child.text or "").strip() for child in element.findall("skos:altLabel", NS)
                         if child.get(XML_LANG) == "en" and (child.text or "").strip()}, key=str.casefold)
    return {"uri": uri, "preferred_label": preferred[0],
            "alternate_labels": [label for label in alternates if label.casefold() != preferred[0].casefold()]}


def read_catalog(stream):
    """Stream an ESCO RDF stream; retain only active English skill concepts."""
    concepts = []
    context = ET.iterparse(stream, events=("start", "end"))
    root = None
    for event, element in context:
        if root is None and event == "start":
            root = element
        if element.tag == "{" + NS["skos"] + "}Concept":
            if event != "end":
                continue
            concept = _parse_concept(element)
            if concept:
                concepts.append(concept)
            if root is not None:
                root.clear()
    return sorted(concepts, key=lambda item: (item["preferred_label"].casefold(), item["uri"]))


def load_catalog(path=None):
    path = Path(path) if path else Path(__file__).parent / "taxonomy" / "esco-review-catalog.json"
    with path.open(encoding="utf-8") as source:
        payload = json.load(source)
    if (not isinstance(payload, dict) or payload.get("esco_version") != ESCO_VERSION or
            payload.get("license") != LICENSE or not isinstance(payload.get("skills"), list)):
        raise ValueError("ESCO review catalog metadata is invalid")
    return payload["skills"]


def exact_candidates(items, catalog):
    """Return exact label candidates only; these never alter scoring taxonomy."""
    index = {}
    for concept in catalog:
        for label in [concept["preferred_label"], *concept["alternate_labels"]]:
            key = normalize_text(label)
            if key:
                index.setdefault(key, []).append(concept)
    results = []
    for item in items:
        key = normalize_text(item["skill"])
        concepts = index.get(key, [])
        if concepts:
            results.append({"unknown_skill": item["skill"], "count": item["count"],
                            "esco_candidates": [{"preferred_label": concept["preferred_label"],
                                "uri": concept["uri"], "matched_label": next(
                                    label for label in [concept["preferred_label"], *concept["alternate_labels"]]
                                    if normalize_text(label) == key)} for concept in concepts[:5]]})
    return results
