# Mit figyel a két detektor — pontosan

A jelenlegi kód alapján, az `app/config.py` alapértékeivel. Ha az értékek változnak,
ez a leírás a logikát írja le, a számok a configból jönnek.

## Ami MINDKETTŐ bemenete

Egyetlen adatforrás: a Binance `aggTrade` folyam a figyelt párokra. Kötésenként
négy adat érkezik:

```
symbol      melyik par
price       a kotes ara
qty         a kotes merete
ts          a tozsdei idobelyeg
buy_taker   az agresszor a vevo volt-e (a "m" mezobol)
```

Ezen kívül fut egy `!bookTicker` feliratkozás (a teljes piac legjobb bid/ask ára),
de abból **csak a spread** használódik, kizárási feltételként.

---

# 1. PUMP/DUMP detektor

**A kérdés, amire válaszol:** „szokatlanul nagyot mozdult-e az ár *ezen a páron*
az elmúlt 2 másodpercben?"

### Amit használ

| adat | használja? |
|---|---|
| `price`, `ts` | **igen** — ez az egyetlen bemenet |
| `qty` (a kötés mérete) | **NEM** |
| `buy_taker` (ki az agresszor) | **NEM** |
| order book mélység | **NEM** (csak a spread, kizárásra) |
| EMA | **NEM** (csak az üzenetbe kerül, információként) |
| a többi pár mozgása | **NEM** |

### Hogyan mér

Páronként egy gördülő lista: az utolsó `2 × moveWindowSec` = **4 másodperc**
`(idő, ár)` párjai. Ebből a **legutolsó 2 másodperc** a mérési ablak.

Az ablak akkor mérhető, ha
- legalább `minTradesInWindow` = **10 kötés** van benne, és
- a kötések legalább `moveWindowSec / 2` = **1 másodpercet** átfognak
  (különben egy ezredmásodperces kötéscsokor „mozgásnak" látszana).

A mozgást **az ablakra illesztett egyenes** adja, nem az első és utolsó ár
különbsége:

```
mozgas %  =  (illesztett meredekseg) × (ablak hossza) / (atlagar) × 100
```

Így egyetlen kiugró print nem tud jelzést csinálni, és a fűrészfog (fel-le-fel-le)
mérése nulla körüli.

Emellett kiszámol egy második számot: **a legnagyobb egyetlen árlépés** (két
egymást követő kötés közti legnagyobb ugrás) az illesztett elmozdulás hány
százaléka. Ez fogja meg azt, amikor egy kötés átsöpri a könyvet, és utána minden
kötés az új áron nyomtat.

### A „normál" (baseline) — a pár saját mércéje

Nincs fix küszöb. Páronként **másodpercenként egy mintát** veszünk az aktuális
2 másodperces ablak `|mozgásából|`, és `baselineMinutes` = **5 percig** tartjuk.
A normál ezek **mediánja** (nem átlag: egy kiugró érték ne vigye el).

Amíg nincs legalább **60 minta** (kb. 1 perc), a pár **nem jelezhet**.

### Mikor lesz jelzés — minden feltételnek teljesülnie kell

1. van már normál (≥60 minta)
2. `|mozgás| ≥ max(minMovePct, normál × baselineRatio)` — alapon `max(0.80%, normál × 8)`
3. ezen a páron `symbolCooldownSec` = **900 mp** óta nem volt jelzés
4. a legnagyobb egyetlen árlépés ≤ `maxSingleStepPct` = **35%**-a a mozgásnak
5. a pár kereskedhető: nincs feketelistán, spread ≤ `maxSpreadPct` = **0.05%**

**Irány:** a mozgás előjele. `confirmSec = 0`, tehát a jelzés **azonnal** megy.

---

# 2. REVERSAL (forduló) detektor

**A kérdés, amire válaszol:** „volt egy érdemi lemozgás (vagy felmozgás), és most
fordul vissza?"

### Amit használ

| adat | használja? |
|---|---|
| `price`, `ts` | **igen** — az alakzathoz |
| `qty` + `buy_taker` | **igen** — a kötésáramláshoz (az utolsó 3 mp-ben) |
| order book mélység | **NEM** (a fal csak az üzenetbe kerül) |
| EMA | **NEM** |
| a többi pár mozgása | **NEM** |

### 1. lépés — volt-e érdemi mozgás (`_find_setup`)

Páronként egy **20 másodperces** (`windowSeconds`) gördülő kötés-ablak.

Az ablakot `wickSliceSec` = **0.5 másodperces szeletekre** vágjuk, és szeletenként
a **medián árú kötést** vesszük. (Nem a nyers min/max: egy pillanat alatt beérkező
pár print — egy nagy kötés, ami átsöpri a könyvet — így nem lesz a mozgás
kezdőpontja.)

Ezekből a pontokból:
- ha a **minimum a maximum UTÁN** keletkezett → lemozgás → **LONG jelölt**
- ha a **maximum a minimum UTÁN** keletkezett → felmozgás → **SHORT jelölt**

A mozgás akkor „érdemi", ha
`mozgás ≥ max(minMovePct, normál_skálázott × baselineRatio)` — alapon
`max(2.00%, normál_skálázott × 8)`.

A `normál_skálázott` a **pump/dump baseline-ja**, átskálázva a mozgás tényleges
hosszára: `normál × √(időtartam / 2 mp)`. Bolyongásnál az elmozdulás az idő
gyökével nő, tehát egy 20 másodperces mozgásnál a normál is nagyobb.

### 2. lépés — az alakzat követése (`_track_setup`)

```
   origin  ─────────────────────────  100%   ← innen indult a mozgás
                                       25%   ← eddig lehet belépni (maxRetracementPct)
                                       12%   ← eddig kell visszapattannia (bounceOfMovePct)
   szélsőérték ───────────────────────  0%
```

- Ha a szélsőérték **6 másodpercnél** (`maxExtremeAgeSec`) régebbi, az alakzatot eldobjuk.
- Ha új, a mozgás **2%**-ánál (`newExtremeOfMovePct`) mélyebb szélsőérték jön, az
  alakzat újraindul — nem forduló volt, hanem folytatódik a mozgás.
- `peak` = a szélsőérték óta elért legjobb ár. Ha a visszapattanás eléri a mozgás
  **12%**-át (`bounceOfMovePct`), és onnan **30%**-ot (`pullbackOfBouncePct`)
  visszahúz, akkor a `peak` **rögzül micro szintként**. (Ettől lesz egy csúcsból
  swing-csúcs: utána visszahúzás következett.)

### 3. lépés — mikor lesz jelzés

Minden feltételnek teljesülnie kell:

1. a micro szint rögzült
2. a szélsőérték ≤ **6 mp** régi
3. a jelenlegi ár a mozgásnak legfeljebb **25%**-át tette vissza (`maxRetracementPct`)
4. az ár **áttörte** a micro szintet a mozgás **5%**-ával (`breakOfMovePct`)
5. **kötésáramlás** az utolsó `flowWindowSeconds` = **3 mp**-ben:
   - legalább **5 kötés** (`minTradesInFlowWindow`)
   - a domináns oldal **USDT-ben** mérve ≥ **1.6×** (`minFlowRatio`)
   - a domináns oldalnak **kötésszámban is** vezetnie kell (egy bálna-print ne
     csináljon fordulást)
   - és a domináns oldalnak **egyeznie kell a jelzés irányával** (LONG-hoz vételi)
6. ezen a páron `cooldownSec` = **1800 mp** óta nem volt forduló-jelzés

`confirmSec = 0`, tehát a jelzés az áttörés pillanatában megy.

---

# Amit egyik detektor sem néz

- **A kötések méretét a pump/dump eldobja** (a `Trade.qty` nincs használva benne).
  Így nem tudja megkülönböztetni a nagy pénzből jövő mozgást a vékony könyvön
  átcsúszó ártól.
- **Az agresszor oldalát** a pump/dump szintén nem nézi (csak a forduló, és ott is
  csak az utolsó 3 másodpercben).
- **Az order book mélységét** egyik sem használja döntésre — a fal csak
  információként kerül az üzenetbe.
- **Az EMA-t** egyik sem használja döntésre.
- **A többi pár mozgását** egyik sem nézi: ha az egész piac egyszerre esik, mindkét
  detektor pár-specifikus jelzésként kezeli.
- **Nincs semmilyen historikus adat, tanulás vagy előrejelzés.** A rendszer csak
  azt állítja, hogy MOST valami szokatlan történt — arról nem mond semmit, hogy
  ezután mi fog.
