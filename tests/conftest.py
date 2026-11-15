"""Configurazione dei test.

I moduli "puri" dell'integrazione (energy_stats, models, const) non importano
Home Assistant. Per provarli senza installare Home Assistant li carichiamo
attraverso un pacchetto fittizio, `oei_pure`, che punta alla stessa cartella
dell'integrazione ma non esegue il suo `__init__.py` (quello sì importa HA).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = ROOT / "custom_components" / "octopus_energy_italia"

if "oei_pure" not in sys.modules:
    _package = types.ModuleType("oei_pure")
    _package.__path__ = [str(COMPONENT_DIR)]
    sys.modules["oei_pure"] = _package

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
