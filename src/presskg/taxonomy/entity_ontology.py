
"""Ontologia delle entità per Knowledge Graph da rassegne stampa."""

import re
import unicodedata


VERSION = "press-entities-v1.1-flat-types"

# 31 categorie canoniche, 7 famiglie.
FAMILIES = {
    "actor": (
        "Person",
        "Company",
        "Organization",
        "PublicAuthority",
        "NonProfitOrganization",
        "PoliticalParty",
        "LaborUnion",
        "MediaOutlet",
        "PopulationGroup",
    ),
    "place_asset": (
        "GPE",
        "Location",
        "NaturalResource",
        "Facility",
        "Infrastructure",
    ),
    "service_product": (
        "Service",
        "Project",
        "Product",
        "Technology",
    ),
    "institutional": (
        "Law",
        "Regulation",
        "Policy",
        "Contract",
        "Document",
        "MediaWork",
    ),
    "semantic": (
        "Event",
        "Topic",
    ),
    "value_time": (
        "Date",
        "Time",
        "Money",
        "Quantity",
    ),
    "other": (
        "Other",
    ),
}


TYPE_TO_FAMILY = {
    entity_type: family
    for family, values in FAMILIES.items()
    for entity_type in values
}

TYPES = frozenset(TYPE_TO_FAMILY)


DESCRIPTIONS = {
    "Person": "persona fisica",
    "Company": "azienda o società commerciale",
    "Organization": "organizzazione di natura non specificata",
    "PublicAuthority": "ente o autorità pubblica",
    "NonProfitOrganization": "organizzazione non profit",
    "PoliticalParty": "partito politico",
    "LaborUnion": "sindacato",
    "MediaOutlet": "testata, emittente o agenzia di stampa",
    "PopulationGroup": "gruppo di cittadini o comunità",

    "GPE": "comune, provincia, regione o Stato geografico",
    "Location": "luogo non amministrativo",
    "NaturalResource": "fiume, falda, bacino o risorsa naturale",
    "Facility": "impianto fisico, edificio, depuratore o centrale",
    "Infrastructure": "rete o sistema infrastrutturale",

    "Service": "servizio erogato",
    "Project": "progetto o programma organizzato",
    "Product": "prodotto materiale",
    "Technology": "tecnologia o sistema tecnico",

    "Law": "legge specifica",
    "Regulation": "regolamento o normativa",
    "Policy": "politica o indirizzo formalizzato",
    "Contract": "contratto, concessione o accordo",
    "Document": "documento o atto identificabile",
    "MediaWork": "articolo o opera mediatica specifica",

    "Event": "accadimento concreto in un contesto temporale",
    "Topic": "tema astratto, non un episodio",

    "Date": "data o periodo",
    "Time": "orario specifico",
    "Money": "importo monetario",
    "Quantity": "quantità o misura",

    "Other": "entità non coperta dalle altre categorie",
}


def _key(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFKD",
        value.strip(),
    )

    ascii_value = "".join(
        char
        for char in normalized
        if not unicodedata.combining(char)
    )

    return re.sub(
        r"[^a-z0-9]",
        "",
        ascii_value.casefold(),
    )


# Sinonimi conservativi italiani/inglesi.
# Non mappare categorie ambigue su tipi più specifici.
_RAW_ALIASES = {
    # Persone e aziende
    "Persona": "Person",
    "Individual": "Person",
    "Azienda": "Company",
    "Impresa": "Company",
    "Società": "Company",
    "Corporation": "Company",
    "Business": "Company",

    # Organizzazioni
    "Org": "Organization",
    "Organizzazione": "Organization",
    "Organisation": "Organization",
    "Institution": "Organization",
    "Istituzione": "Organization",
    "Agency": "Organization",

    # Enti pubblici
    "GovernmentAgency": "PublicAuthority",
    "GovernmentBody": "PublicAuthority",
    "PublicInstitution": "PublicAuthority",
    "EntePubblico": "PublicAuthority",
    "AutoritàPubblica": "PublicAuthority",
    "PublicAgency": "PublicAuthority",

    # Organizzazioni specializzate
    "NGO": "NonProfitOrganization",
    "ONG": "NonProfitOrganization",
    "Nonprofit": "NonProfitOrganization",
    "Onlus": "NonProfitOrganization",
    "Partito": "PoliticalParty",
    "PartitoPolitico": "PoliticalParty",
    "Sindacato": "LaborUnion",
    "TradeUnion": "LaborUnion",
    "Newspaper": "MediaOutlet",
    "NewsAgency": "MediaOutlet",
    "PressAgency": "MediaOutlet",
    "Testata": "MediaOutlet",
    "TestataGiornalistica": "MediaOutlet",
    "MediaOrganization": "MediaOutlet",
    "CitizenGroup": "PopulationGroup",
    "ResidentGroup": "PopulationGroup",
    "CommunityGroup": "PopulationGroup",

    # Luoghi
    "GeopoliticalEntity": "GPE",
    "City": "GPE",
    "Town": "GPE",
    "Province": "GPE",
    "Region": "GPE",
    "Country": "GPE",
    "GeographicLocation": "Location",
    "Place": "Location",
    "Luogo": "Location",
    "LOC": "Location",

    # Ambiente e infrastrutture
    "NaturalFeature": "NaturalResource",
    "WaterBody": "NaturalResource",
    "River": "NaturalResource",
    "Fiume": "NaturalResource",
    "Building": "Facility",
    "Plant": "Facility",
    "Impianto": "Facility",
    "FAC": "Facility",
    "PhysicalFacility": "Facility",
    "Infrastruttura": "Infrastructure",
    "NetworkInfrastructure": "Infrastructure",
    "ReteIdrica": "Infrastructure",

    # Servizi
    "Servizio": "Service",
    "PublicService": "Service",
    "Progetto": "Project",
    "Programma": "Project",
    "InterventionProject": "Project",
    "Prodotto": "Product",
    "Tecnologia": "Technology",

    # Elementi istituzionali
    "Legge": "Law",
    "Normativa": "Regulation",
    "Regolamento": "Regulation",
    "PublicPolicy": "Policy",
    "PoliticaPubblica": "Policy",
    "Contratto": "Contract",
    "Agreement": "Contract",
    "Concession": "Contract",
    "Concessione": "Contract",
    "Documento": "Document",
    "Report": "Document",
    "Rapporto": "Document",
    "PressRelease": "Document",
    "Article": "MediaWork",
    "NewsArticle": "MediaWork",

    # Eventi e argomenti
    "Evento": "Event",
    "Incident": "Event",
    "Occurrence": "Event",
    "Episodio": "Event",
    "Accadimento": "Event",
    "Tema": "Topic",
    "Argomento": "Topic",
    "Theme": "Topic",

    # Tempo e valori
    "Data": "Date",
    "Periodo": "Date",
    "Datetime": "Date",
    "Orario": "Time",
    "MonetaryAmount": "Money",
    "Importo": "Money",
    "ValoreMonetario": "Money",
    "Amount": "Quantity",
    "Misura": "Quantity",
    "Percent": "Quantity",
    "Percentage": "Quantity",

    "Altro": "Other",
    "Misc": "Other",
}


ALIASES = {
    _key(alias): canonical
    for alias, canonical in _RAW_ALIASES.items()
}

CANONICAL = {
    _key(entity_type): entity_type
    for entity_type in TYPES
}


def normalize_type(raw: object) -> str:
    """
    Normalizza soltanto sinonimi riconosciuti.
    Conserva il valore originale dei tipi sconosciuti.
    """
    if not isinstance(raw, str):
        return ""

    value = raw.strip()

    if not value:
        return ""

    key = _key(value)

    return (
        CANONICAL.get(key)
        or ALIASES.get(key)
        or value
    )


def type_family(entity_type: str) -> str:
    return TYPE_TO_FAMILY.get(
        entity_type,
        "unmapped",
    )



def prompt_catalog() -> str:
    """
    Espone al modello soltanto i tipi canonici.
    Le famiglie restano utilizzabili internamente
    da Python, ma non sono valori ammessi per type.
    """

    lines = [
        "TIPI CANONICI CONSENTITI:",
        ", ".join(sorted(TYPES)),
        "",
        "REGOLE:",
        "Il campo type deve essere ESATTAMENTE uno",
        "dei tipi elencati sopra.",
        "Non utilizzare categorie generiche come",
        "actor, place_asset, service_product,",
        "institutional, semantic o value_time.",
        "Non inventare nuovi tipi o ruoli.",
        "",
        "DEFINIZIONI:",
    ]

    for name in sorted(DESCRIPTIONS):
        lines.append(
            f"- {name}: {DESCRIPTIONS[name]}"
        )

    return "\n".join(lines)
