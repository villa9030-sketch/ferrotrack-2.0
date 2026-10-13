"""Cache in memoria con un tetto in NUMERO e in BYTE (LRU).

Dalla prova notturna (12/10): il server e' passato da 150 a 300 MB in 13 ore.
Le cache delle anteprime SVG e della geometria del CAD avevano un tetto solo
nel numero di voci (128): con disegni grandi una voce pesa anche qualche MB,
e 128 voci potevano diventare centinaia di MB. Qui conta anche il peso.
"""
import threading
from collections import OrderedDict


class CacheLimitata:
    def __init__(self, max_voci: int, max_byte: int):
        self.max_voci = max_voci
        self.max_byte = max_byte
        self._d = OrderedDict()
        self._byte = 0
        self._lock = threading.Lock()

    @staticmethod
    def _peso(valore) -> int:
        try:
            return len(valore)
        except TypeError:
            return 0

    def get(self, chiave, predefinito=None):
        with self._lock:
            v = self._d.get(chiave)
            if v is None:
                return predefinito
            self._d.move_to_end(chiave)
            return v[0]

    def put(self, chiave, valore):
        peso = self._peso(valore)
        if peso > self.max_byte // 4:
            return                    # troppo grosso: non si tiene
        with self._lock:
            vecchio = self._d.pop(chiave, None)
            if vecchio is not None:
                self._byte -= vecchio[1]
            self._d[chiave] = (valore, peso)
            self._byte += peso
            while self._d and (len(self._d) > self.max_voci or self._byte > self.max_byte):
                _, (_, p) = self._d.popitem(last=False)
                self._byte -= p

    def pop(self, chiave, predefinito=None):
        with self._lock:
            v = self._d.pop(chiave, None)
            if v is None:
                return predefinito
            self._byte -= v[1]
            return v[0]

    def clear(self):
        with self._lock:
            self._d.clear()
            self._byte = 0

    def keys(self):
        with self._lock:
            return list(self._d.keys())

    def __contains__(self, chiave):
        with self._lock:
            return chiave in self._d

    def __len__(self):
        return len(self._d)

    @property
    def byte(self) -> int:
        return self._byte

    def __iter__(self):
        return iter(self.keys())
