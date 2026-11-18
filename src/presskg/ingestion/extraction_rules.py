
"""
Regole di estrazione per rassegne stampa italiane.
"""

import re
import unicodedata


VERSION = "press-extraction-rules-v2"


EXTRACTION_GUARDRAILS = """
REGOLE AGGIUNTIVE OBBLIGATORIE

RUOLI SEMANTICI

1. Identifica chi compie l'azione, chi la riceve
   e chi ne è eventualmente beneficiario.

2. Non confondere il destinatario con l'agente.
   Una denuncia presentata a un'autorità non
   significa che l'autorità abbia denunciato.

3. La direzione del predicato deve rispettare
   esplicitamente il significato della frase.

4. I predicati inversi, come FINANCED_BY
   e AFFECTED_BY, mantengono il loro verso.

PREDICATI

5. Non usare COORDINATES quando l'evidenza
   indica soltanto presenza o partecipazione.

6. Non usare CAUSES quando il testo indica
   solo un'associazione temporale.

7. Non usare MANAGES quando il soggetto
   è semplicemente menzionato in relazione
   a un servizio o impianto.

8. Non trasformare proposte, obiettivi
   o programmi in azioni già completate.

9. Preferisci predicati specifici, come
   REDUCES, ENABLES, REQUIRES, RECEIVES,
   PRESENT_AT, COORDINATES_WITH,
   FINANCED_BY, REPRESENTS.

10. Evita HAS, IS, OPINION e OPINIONS
    come predicati tra entità.

ATTRIBUZIONI E MODALITÀ

11. Se una dichiarazione è attribuita
    a una persona o organizzazione,
    conserva attribution.

12. Se il testo contiene un'accusa,
    utilizza alleged quando appropriato.
    Non trasformarla in asserted.

13. Una smentita non dimostra
    che il fatto smentito sia accaduto.

14. Distingui sempre fatto accertato,
    dichiarazione riportata, ipotesi,
    accusa e opinione.

15. Per alleged, denied e opinion
    individua la fonte, quando presente.

EVIDENZE E COREFERENZE

16. evidence_id identifica il segmento
    principale che supporta la relazione.

17. Se il soggetto è identificabile solo
    in un altro segmento della finestra,
    fornisci anche context_evidence_id.

18. context_evidence_id è facoltativo,
    ma deve essere un ID S001, S002, ...
    realmente presente nell'input.

19. Se non puoi identificare con sicurezza
    un referente, non inventarlo.

20. Non inventare date, nomi, ruoli,
    responsabilità o relazioni.
"""


def normalize_actor_name(name):
    value = unicodedata.normalize("NFKD", str(name or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^\w]+", " ", value)

    return re.sub(r"\s+", " ", value).strip()
