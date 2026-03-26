"""Pre-scrape data cache for LLM analysts."""

# Standard fuzzy matching cutoff used across all scrapers.
# A single value (0.72) balances precision (avoiding false matches) with recall
# (catching name variations like "UConn" vs "Connecticut").
# The only exception is slug-based fallback matching in BartTorvik (0.65),
# which intentionally uses a lower cutoff because slug names are already
# partially normalized.
FUZZY_MATCH_CUTOFF = 0.72
FUZZY_MATCH_CUTOFF_SLUG = 0.65
