"""FCM topic names. Must stay identical to notificationTopics.js in the app.

FCM topic names are case-sensitive, so "legislativeNews" and
"LegislativeNews" are different topics.
"""

# FCM topic prefix per country. The app subscribes to "<prefix><Branch>News",
# with no prefix for the US.
COUNTRY_PREFIXES = {
    "us": "",
    "ca": "Canada",
    "mx": "Mexico",
    "uk": "UK",
}

BRANCH_TOPICS = {
    "legislative": "LegislativeNews",
    "executive": "ExecutiveNews",
    "judicial": "JudicialNews",
    "defense": "DefenseNews",
    "defence": "DefenseNews",
    "economy": "EconomyNews",
    "environment": "EnvironmentNews",
    "foreign affairs": "ForeignAffairsNews",
    "foreignaffairs": "ForeignAffairsNews",
    "health": "HealthNews",
    "elections": "ElectionNews",
    "science & tech": "ScienceTechNews",
    "sciencetech": "ScienceTechNews",
    "science": "ScienceTechNews",
    "spending": "SpendingNews",
    "infrastructure": "InfrastructureNews",
    "regulations": "RegulationsNews",
    "justice": "JusticeNews",
    "cyber": "CyberNews",
}

DEFAULT_TOPIC = "GovGlanceNews"

# The new notification system sends to versioned topics ("v2-ExecutiveNews").
# App versions from before it are subscribed to the plain names and never get
# these pushes; app versions with it leave the plain names on first launch.
# Must match TOPIC_VERSION in the app's notificationTopics.js.
TOPIC_VERSION = "v2-"


def country_code(schema):
    """Map a country_schema value (or a short code) to us / ca / mx / uk.

    Matches the app's getCountryFromSchema for the four countries the app has
    notification bells for. Returns None for any other country (for example
    "australia"), since nobody in the app can be subscribed to its topics.
    """
    # Rows hold either a schema name ("united_states_of_america") or a display
    # name ("United Kingdom", from the news crawler), so spaces and underscores
    # are treated the same.
    value = (schema or "us").strip().lower().replace(" ", "_")
    if value in COUNTRY_PREFIXES:
        return value
    if value in ("united_states_of_america", "united_states", "usa"):
        return "us"
    if "canada" in value or value.startswith("ca_"):
        return "ca"
    if "mexico" in value or value.startswith("mx_"):
        return "mx"
    if "united_kingdom" in value or value == "great_britain" or value.startswith("uk_"):
        return "uk"
    return None


def build_push_topic(branch, country="us"):
    """Return the FCM topic for a branch and country ("v2-ExecutiveNews"), or
    None if the country has no topics."""
    code = country_code(country)
    if code is None:
        return None
    name = BRANCH_TOPICS.get((branch or "").strip().lower(), DEFAULT_TOPIC)
    return f"{TOPIC_VERSION}{COUNTRY_PREFIXES[code]}{name}"