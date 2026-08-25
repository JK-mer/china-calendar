"""Cluster grouping shared by the MCP server and the web dashboard: events
read as a brief (German institutional / EU / business formats / China), not a
flat list."""

from __future__ import annotations

from .models import Event

CLUSTERS = {
    "german_institutional": {"Bundestag", "Bundesrat", "BMWE", "AA", "Auswärtiges Amt",
                             "Bundesregierung"},
    "eu": {"EU_Council", "EU Council", "European Council", "European Commission",
           "European Parliament"},
    "business_formats": {"APA", "OAV", "BDI", "DIHK", "GTAI", "AHK", "BGA", "IISS"},
    "china": {"NPC", "NPCSC", "CPPCC", "CCP", "State_Council", "State Council",
              "MOFCOM", "MFA", "Boao_Forum"},
}

CLUSTER_LABELS = {
    "german_institutional": "German institutional",
    "eu": "EU",
    "business_formats": "Business formats",
    "china": "China",
    "elections": "Elections",
    "other": "Other",
}

# Sector fallback in priority order — elections first, so a Landtagswahl
# (sectors: elections + german_institutional) clusters as an election.
_SECTOR_CLUSTERS = ("elections", "german_institutional", "business_formats")


def cluster_of(event: Event) -> str:
    for cluster, actors in CLUSTERS.items():
        if set(event.actors) & actors:
            return cluster
    for sector in _SECTOR_CLUSTERS:
        if sector in event.sectors:
            return sector
    return "other"
