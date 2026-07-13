"""Automated cross-venue contract mapping.

Discovers markets on both venues, matches the ones that are the *same underlying
contract* (recording YES/NO polarity), and continuously refreshes the mapping —
adding new pairs and retiring settled/delisted ones.

Pipeline: discovery -> blocking -> scoring -> adjudication -> tiered storage.
"""

from .models import MappingVerdict, VenueMarket

__all__ = ["VenueMarket", "MappingVerdict"]
