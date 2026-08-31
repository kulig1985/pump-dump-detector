# Paraméterek — mit jelent, és mi történik, ha átállítod

## Hol állítod: `app/config.py`

**Az alapértékek a kódban vannak, és hidegindítással minden beállítás felépül.**
A MongoDB `config` collection csak egy másolat, amit induláskor a kód hoz létre:

```
app/config.py            ->  config collection (MongoDB)
  MARKET_DEFAULTS              market     KÖZÖS: melyik párokat figyeljük
  DETECTOR_DEFAULTS            detector   CSAK a pump/dump
  REVERSAL_DEFAULTS            reversal   CSAK a forduló
  TRADING_DEFAULTS             trading    pozíciónyitás (alapból KI)
  TELEGRAM_DEFAULTS            telegram   a bot és az üzenet
```

A hangolás **mindig a kódban** történik:

```bash
# 1. atirod az erteket az app/config.py-ban
# 2. ujrainditas
git pull && docker compose up -d --build
```

A `config` collectiont bármikor törölheted: a következő induláskor **minden
beállítás újra létrejön** a kódban lévő alapértékekkel. Ezt teszt őrzi
(`test_cold_start_creates_every_setting`) — egyetlen beállítás sem maradhat ki
hidegindításnál, és semmi nem várhat kézi beavatkozásra.

Megnézni, mi fut épp:

```js
use("pump-dump")
db.config.findOne({_id: "detector"})
```

---

## Az alapelv: a pár saját normálja

Nincs fix „0.3% = pump" szabály. Minden pár **önmagához** van mérve.

A rendszer futás közben megméri, mennyi az adott páron a **normál** 2 másodperces
elmozdulás (medián, 5 perc visszatekintéssel). Ez a `normal` a logban.

```
BTCUSDT   normál 2 mp-es mozgása:  0.012%   →  jelzéshez kell: 0.012 × 6 = 0.072%,
                                                de az abszolút padló 0.50%  →  0.50%
MEMEUSDT  normál 2 mp-es mozgása:  0.180%   →  jelzéshez kell: 0.180 × 6 = 1.08%
```

Ezért nem jelez a meme coin folyamatos csapkodására: ott a *normál* is nagy, tehát
a küszöb is nagy. A képlet:

```
   kell  =  max( minMovePct ,  baselineRatio × normál )
```

Amíg egy párnak nincs elég mintája (kb. 1–2 perc), **nincs jelzés** — nem tippelünk.

---

# `market` — közös piaci beállítások

Ezek döntik el, **melyik párokra iratkozunk fel a WebSocketen**. Onnantól mindkét
detektor pontosan ugyanazt a kötésfolyamot kapja, ezért nincs belőlük külön példány
a detektoroknál.

### `enabled` — alap: `true`
Az egész feldolgozás ki-/bekapcsolása. `false` esetén a WebSocket él, de egyetlen
kötést sem dolgozunk fel. Vészfék.

### `quoteAssets` — alap: `["USDT", "USDC"]`
Milyen elszámoló devizás párokat figyelünk.
> **Példa:** `["USDT"]` — csak a USDT párok. A `BTCUSDC`, `ETHUSDC` kiesik, jellemzően
> 30–40 párral kevesebbet nézünk.

### `minQuoteVolume24h` — alap: `120 000 000`
Ennél kisebb 24 órás forgalmú (USDT-ben mért) párok kiesnek. Ez a **likviditási szűrő**:
kis forgalmú páron egy nagyobb megbízás magától elviszi az árat.
> **Példa:** `120000000` (120M) mellett kb. 60–90 pár marad a Binance ~500 perpetual
> párjából. `500000000`-ra emelve már csak a top 20–30 (BTC, ETH, SOL, XRP…).
> `20000000` mellett bejönnek a kisebb altok is — több jelzés, több zaj.

### `maxSymbols` — alap: `60`
A forgalom szerint rendezett lista első ennyi eleme. Biztonsági plafon a WS terhelésre.
> **Példa:** ha `minQuoteVolume24h` 300 párt engedne át, de itt `60` áll, a 60
> legforgalmasabbat nézzük.

### `symbolRefreshMinutes` — alap: `60`
Ennyi percenként kérjük le újra a forgalmi listát, és iratkozunk fel az újakra.
> **Példa:** `15` — negyedóránként frissül. Akkor hasznos, ha egy friss listing
> forgalma hirtelen felszalad, és hamar be akarod venni.

### `symbolWhitelist` — alap: `[]`
**Ha nem üres, KIZÁRÓLAG ezeket figyeljük** — a forgalmi szűrő ilyenkor nem számít.
> **Példa:** `["BTCUSDT","ETHUSDT","SOLUSDT"]` — tesztelésre tökéletes: három páron
> nézed a logot, és át tudod látni, mi történik.

### `symbolBlacklist` — alap: `[]`
Ezeket sosem figyeljük, akkor sem, ha a forgalmuk átmenne.
> **Példa:** `["1000PEPEUSDT","1000BONKUSDT"]` — ha egy konkrét pár idegesít.

### `maxSpreadPct` — alap: `0.05`
A legjobb vételi és a legjobb eladási ár közti rés felső határa **százalékban**.
Ennél szélesebb páron nincs jelzés, mert a be- és kiszállás felemésztené a mozgást.
> **Példa:** bid 0.9998 / ask 1.0002 → a rés 0.0004, azaz **0.04%** → átmegy.
> Ha bid 0.9990 / ask 1.0010 → 0.20% → kiesik `spread_too_wide` okkal.
> A STATUS sor kiírja a mezőny eloszlását, abból látod, hova érdemes tenni.

### `outcomeMinutes` — alap: `[1, 5, 15]`
A jelzés **után** ennyi perccel jegyezzük fel, hol áll az ár, és beírjuk ugyanabba a
signal dokumentumba. **Semmit nem kapuz** — nem backteszt, nem jelző, nem nyit pozíciót.
Ez az egyetlen módja megtudni, hogy egy forduló vagy egy dump tartós-e.
> **Példa:** egy LONG jelzés 100.00-nál. 5 perc múlva az ár 100.62 →
> `outcome.m5 = {price: 100.62, pct: +0.62}`. SHORT jelzésnél az előjel meg van
> fordítva: **pozitív mindig azt jelenti, hogy a jelzés irányába ment az ár.**
> A STATUS blokkban és a Telegram életjelben összegezve is látod (lásd lentebb).
>
> Az árat a már futó kötésfolyamból vesszük (utolsó ár páronként) — nulla extra
> hálózati kérés. Ha egy párról épp nem érkezik kötés, azt a mérést kihagyjuk,
> nem találunk ki adatot.
> Lekérdezés: `db.signals.find({"outcome.m5.pct": {$lt: 0}})` — ami rossz irányba ment.

### `statusIntervalSec` — alap: `60`
Ennyi másodpercenként egy STATUS sor a logba.
> **Példa:** `30` — sűrűbb visszajelzés hangolás közben. `300` — nyugodtabb log.

---

# `detector` — pump / dump

Azt keresi, hogy **rendkívüli-e a mozgás ezen a páron**, néhány másodperces ablakban.

### `enabled` — alap: `true`
Csak ezt a detektort kapcsolja ki/be. A forduló detektor tovább fut.

## A mérés

### `moveWindowSec` — alap: `2.0`
Ekkora **időablakban** mérjük az elmozdulást. Nem gyertya — nem várunk zárásra.
A mozgást az ablakra **illesztett egyenes** adja, nem az első és utolsó ár különbsége:
így egyetlen kiugró print nem tud jelzést csinálni, és a fűrészfog (fel-le-fel-le)
mérése ~nulla.
> **Példa:** `2.0` mellett a „hirtelen" azt jelenti: 2 másodperc alatt. `5.0` mellett
> lassabb, nagyobb ívű mozgásokat keres, és kevesebb pillanatnyi kilengést lát meg.

### `minTradesInWindow` — alap: `10`
Ennyi kötésnek kell lennie az ablakban, különben nem mérhető. Ezen felül a kötéseknek
szét kell terülniük az ablak legalább felén.
> **Példa:** a nagy párokon 30 kötés beérkezik 30 ezredmásodperc alatt is. Abból
> nem lehet tempót számolni — ez a feltétel dobja el az ilyen kötéscsokrokat.

### `baselineMinutes` — alap: `5`
Ennyi perc visszatekintésből épül a pár normálja.
> **Példa:** `5` → 300 minta mediánja. `2` → gyorsabban alkalmazkodik egy hirtelen
> felélénkülő párhoz, de zajosabb. `15` → nagyon stabil, de lassan követi a piacot.

## Érzékenység — **ezeket állítsd**

### `baselineRatio` — alap: `8.0`  ⭐ **a fő kapcsoló**
A mozgás a pár normáljának ennyiszerese legyen.
> **Példa:** a pár normálja 0.05%.
> `4.0` → 0.20% már jelzés (sok jelzés, sok zaj)
> `6.0` → 0.30% kell
> `10.0` → 0.50% kell (ritka, de tényleg rendkívüli)

### `minMovePct` — alap: `0.80`  ⭐
Abszolút padló: ennél kisebb mozgás **sosem** jelzés, akármilyen nyugodt a pár.
Ez véd a hidegindulástól is (amikor a normál még 0.001%, és minden „266×"-nek látszik).
> **Példa:** BTC normálja 0.012% → a `baselineRatio` csak 0.072%-ot követelne, ami
> díjjal (oda-vissza 0.10%) veszteséges. A `0.50` padló emiatt van.

### `maxSingleStepPct` — alap: `35`
Ha a mozgás ennél nagyobb részét **egyetlen árlépés** adta, nem jelzés.
> **Példa:** egy nagy kötés átsöpri a könyv 5 szintjét, az ár 100.0-ról 100.4-re ugrik,
> és a következő 30 kötés már 100.4-en nyomtat. Az ablak „szép egyenletes +0.4%-nak"
> látszik, pedig egyetlen lépcső az egész — és az ilyen ár rendszerint visszaesik.
> `0` = kikapcsolva.

### `confirmSec` — alap: `60.0`  ⭐
A jelzés **nem** a mozgás pillanatában megy ki. Ennyi ideig **végig** tartania kell
a mozgásnak — nem egy pillantás a végén, hanem **minden kötésnél** ellenőrizzük.
Amint visszaesik, azonnal eldobjuk (a határidőt sem várjuk ki).
> **Példa (valódi eset, ZECUSDT):** a kanóc 15 másodperccel később **még fent volt**,
> és csak utána csorgott vissza. Egy végponti pillantás ezt átengedte — a folyamatos
> ellenőrzés + a 60 másodperc kifogja.
> Cserébe a jelzés egy perccel később ér oda, és elveszíted azokat a valódi
> mozgásokat, amelyek egy percen belül lefutnak. Ez a szándék: amit ennyi idő alatt
> visszavernek, azt úgysem tudod kézzel lekereskedni.

### `confirmHoldPct` — alap: `80`
És a látott mozgás ennyi százaléka legyen meg — **a `confirmSec` teljes ideje alatt,
végig**. Nem elég, ha a végén épp jó: egy megbicsaklás menet közben is eldobja.
> **Példa:** az ablak eleje 100.00, a jelzés pillanatában 100.30 (a látott mozgás
> tehát 0.30). A következő 60 másodpercben végig:
> 100.30 → 100% megvan → **jelzés**
> 100.26 → 87% → **jelzés**
> 100.22 → 73% → nincs jelzés (80% kell)
> 100.12 → 40% → nincs jelzés, a logban: `VISSZAESETT ... pillanatnyi korrekcio volt`
> 100.00 → 0% → nincs jelzés

### `symbolCooldownSec` — alap: `900`
Páronként ennyi szünet két jelzés között.
> **Példa:** `900` = ugyanaz a pár legfeljebb 15 percenként jelezhet. Egy elnyúló
> pumpból így egy jelzést kapsz, nem tizenötöt.

## Csak információ az üzenetben (semmit nem kapuznak)

### `orderBookLevels` — alap: `20`
Triggerkor ennyi árszintet kérünk le a könyvből.

### `wallSensitivity` — alap: `3.0`
Fal = olyan árszint, ahol a többi szint **mediánjának** ennyiszerese áll.
> **Példa:** 20 szinten átlagosan 5 000 USDT áll, egy szinten 40 000 → az arány 8×,
> `3.0` mellett ez fal. Az üzenetben: `sell wall 0.10% tavolsagra`.

### `wallMaxDistancePct` — alap: `1.5`
Ennél távolabbi falat meg sem említünk — nem befolyásol egy másodperces mozgást.

### `emaFast` / `emaSlow` / `emaInterval` — alap: `9` / `21` / `1m`
1 perces EMA9 és EMA21 az üzenetbe, `bullish` / `bearish` szöveggel.
**Sosem trigger és sosem szűrő** — csak kontextus.

---

# `reversal` — lokális forduló

Azt keresi, hogy egy lemozgás (vagy felmozgás) után **megfordult-e** az ár.

```
   origin ────────────────────────────  100%   ← innen indult a lemozgás
                                         25%   ← eddig lehet belépni (maxRetracementPct)
                                         12%   ← eddig kell visszapattannia (bounceOfMovePct)
   mélypont ──────────────────────────    0%
```

`LEMOZGÁS → MÉLYPONT → VISSZAPATTANÁS → MICRO-HIGH RÖGZÜL → VÉTELI FLOW → ÁTTÖRÉS`

Minden méret **a mozgás arányában** van, nem abszolút százalékban — így egy 1%-os és
egy 5%-os mozgásnál ugyanaz a logika működik.

### `enabled` — alap: `true`
Csak ezt a detektort kapcsolja ki/be.

## Érzékenység — **ezeket állítsd**

### `minMovePct` — alap: `2.00`  ⭐ **a legerősebb szűrő**
Mekkora előzetes mozgás után van egyáltalán értelme fordulót keresni.
> **Példa:** egy 0.3%-os hullámzásból nincs mit kifordulni — a „forduló" ott csak
> zaj. `2.00` azt mondja: legalább 2%-ot kellett esnie (vagy emelkednie) az árnak
> a 20 másodperces ablakban, mielőtt fordulóról beszélünk.
> `1.0` → gyakoribb, kisebb fordulók is bejönnek.

### `baselineRatio` — alap: `8.0`  ⭐
És ugyanez a pár normáljához mérve. A normál a mozgás **tényleges hosszára**
skálázódik: bolyongásnál az elmozdulás az idő gyökével nő.
> **Példa:** a normál 2 másodpercre 0.05%. Egy 20 másodperces mozgásnál a normál
> `0.05 × √(20/2) = 0.158%`, tehát ott `0.158 × 8 = 1.26%` kell. Enélkül egy lassú
> 20 másodperces kúszás is „rendkívülinek" látszana.

### `confirmSec` — alap: `30.0`  ⭐
Az áttörés pillanata még nem forduló. Ennyi ideig **végig** a micro szint túloldalán
kell maradnia az árnak — minden kötésnél ellenőrizzük, és amint visszaesik a szint
mögé, azonnal eldobjuk.
> **Példa:** a micro-high 0.7838, az ár áttöri 0.7845-ig. A következő 30 másodpercben:
> 0.7850 → tartja → **jelzés**
> 0.7832 → visszaesett a szint mögé → nincs jelzés, a logban:
> `VISSZAESETT ... az attores nem tartott`

### `maxRetracementPct` — alap: `25`  ⭐
A jelzés pillanatáig a mozgásnak legfeljebb ennyi %-a jöhetett vissza.
> **Példa:** 100.0-ról 99.0-ra esett az ár (a mozgás 1.00). Ha a jelzés 99.25-nél
> lenne, az a mozgás 25%-a — épp a határon. 99.40-nél már 40%, tehát a mozgás
> nagy része lefutott: nincs jelzés. Ez véd attól, hogy a dead-cat bounce tetején szállj be.

### `maxExtremeAgeSec` — alap: `6`
A szélsőérték (mélypont / csúcs) ennél frissebb legyen.
> **Példa:** `6` — egy 15 másodperces mélypontra már késő beszállni, a fordulás nagy
> része megtörtént. `4` — csak a nagyon friss fordulókra jelez.

### `cooldownSec` — alap: `1800`
Páronként ennyi szünet.
> **Példa:** `1800` = ugyanarról a párról legfeljebb félóránként egy forduló-jelzés.

## Az alakzat geometriája (ritkán kell hozzányúlni)

### `windowSeconds` — alap: `20`
Ekkora rolling kötés-ablakban keressük az alakzatot. Ennél régebbi kötés kiesik.

### `wickSliceSec` — alap: `0.5`
A szélsőértéket **nem** a nyers minimum/maximum adja, hanem ekkora szeletek középára.
> **Példa (valódi eset):** SKRUSDT-nél egyetlen pillanatban négy print ment le
> 0.015642-ig, majd az ár azonnal visszaállt. A nyers minimum ezt vette a mozgás
> kezdőpontjának, és „0.61% emelkedést" jelentett — valójában az ár 0.01570 és
> 0.015738 között mozgott, azaz 0.24%-ot. A fél másodperces szeletek középárán a
> kanóc eltűnik, egy valódi lemozgás viszont megmarad. `0` = kikapcsolva.

### `bounceOfMovePct` — alap: `12`
Ennyit kell visszapattannia a mozgásból, hogy egyáltalán fordulóról beszéljünk.
> **Példa:** 1.00-s mozgásnál a mélyponttól 0.12-t kell emelkednie.

### `pullbackOfBouncePct` — alap: `30`
A visszapattanásból ennyi visszahúzás **rögzíti a micro szintet** — ettől lesz egy
csúcsból swing-csúcs (utána visszahúzás következett).
> **Példa:** a mélypont 99.00, az ár felpattan 99.15-ig (a visszapattanás 0.15).
> Ha ebből 30%-ot, azaz 0.045-öt visszahúz (99.105-ig), akkor a 99.15 rögzül
> micro-high-ként, és onnantól azt kell áttörni.

### `breakOfMovePct` — alap: `5`
Ekkora áttörés kell a micro szinten, a mozgás arányában.
> **Példa:** 1.00-s mozgásnál a micro-high 99.15 → az áttöréshez 99.20 kell.
> Egy 0.02%-os „áttörés" nem információ, csak zaj.
>
> ⚠️ `bounceOfMovePct + breakOfMovePct` **maradjon jóval `maxRetracementPct` alatt**
> (alapon 12 + 5 = 17 < 25), különben a belépő matematikailag mindig a határon túlra
> esik, és soha nem lesz jelzés.

### `newExtremeOfMovePct` — alap: `2`
Ennyivel mélyebb új minimum indítja újra az alakzatot.
> **Példa:** 1.00-s mozgásnál ha az ár a mélypont alá megy 0.02-vel, akkor nem
> forduló volt, hanem folytatódik a lemozgás — az alakzat nullázódik.

## Kötésáramlás

### `flowWindowSeconds` — alap: `3`
Ekkora ablakban nézzük a vevő/eladó arányt.

### `minFlowRatio` — alap: `1.6`
A fordulat irányába ekkora túlsúly kell, **USDT-ben** mérve (nem darabszámban).
> **Példa:** LONG fordulóhoz az utolsó 3 másodpercben a vételi oldalnak 1.6×-osnak
> kell lennie: 8 000 USDT vétel vs 5 000 USDT eladás → 1.6× → rendben.

### `minTradesInFlowWindow` — alap: `5`
Ennyi kötés kell az ablakba. Emellett a domináns oldalnak **kötésszámban is** vezetnie
kell.
> **Példa:** egy 500 USDT-s vétel és nyolc 10 USDT-s eladás → notionalban 6× vételi
> túlsúly, de a kötések nyolcada vételi. Egyetlen bálna-print nem csinál fordulást.

---

# `telegram`

### `enabled` — alap: `true`
`false` esetén a jelzés a logba és a MongoDB-be megy, Telegramra nem.

### `signalWindowMinutes` — alap: `10`
Ekkora visszatekintéssel írjuk az üzenetbe, hányadik jelzés ez ebbe az irányba.
> **Példa:** `gyakorisag: 3. SHORT 10 percen belul` — ha ez a szám gyorsan nő,
> a rendszer túl érzékenyen van beállítva.

### `statusEveryMinutes` — alap: `20`
Ennyi percenként egy **életjel** üzenet Telegramra: fut-e még, mit néz éppen, és mi
lett az eddigi jelzésekből. `0` = nincs ilyen üzenet.
> **Példa** (20 percenként):
> ```
> 🟦 ELETJEL
> 14:20:03 UTC  ·  3h 12p ota fut
>
>   figyelt par          58 db
>   WS kapcsolat         1/1
>   kotes / perc         1,932
>   jelzes indulas ota   7 db
>   kizarva              kizarva 2: tul szeles a spread: 2
>
> MOST A LEGMOZGEKONYABB
>   SOLUSDT   0.31% / kell 0.80%
>   ZKCUSDT   0.22% / kell 0.80%
>
> LEGKOZELEBB A JELZESHEZ
>   • normal kesz: 56/58 par | legkozelebb: SOLUSDT 0.31% (kell 0.80%, normalja 0.041%)
>
> UTOLSO JELZESEK  (merre indult el az ar)
> ido   par          tipus irany      +1p     +5p    +15p
> 04:49 SOLUSDT      rev   SHORT   +0.98%  +2.24%  +5.49%
> 04:44 BTCUSDT      pump  SHORT   +0.08%  +0.31%     ...
> 04:39 ENAUSDT      pump  LONG    -1.54%  -4.67%  -4.77%
>
> OSSZESITES  (+ = a jelzes iranyaba ment)
>                        +1p     +5p    +15p
> pump_dump   13 jelzes
>   atlag             -0.31%  -0.40%  +0.02%
>   talalat              23%     46%     54%
> reversal     4 jelzes
>   atlag             +0.59%  +1.15%  +1.75%
>   talalat              75%     75%     75%
> ```
> A `MOST A LEGMOZGEKONYABB` nem a legnagyobb abszolút mozgás, hanem ami a **saját
> küszöbéhez** legközelebb van — ebből látod, hogy áll a mezőny a jelzéshez képest.

### `statusRecentSignals` — alap: `5`
Az életjelben ennyi **legutóbbi jelzés** eredménye jelenik meg egyenként.
> **Példa:** `10` — hosszabb lista, több múltbeli jelzéssel.

### `botToken` / `chatId`
A BotFather-től kapott token és a cél chat azonosítója. **Környezeti változóból**
(`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) is jöhet — az API kulcsok nem a DB-ben laknak.

### `chatIds` — alap: `{"pump_dump": "", "reversal": ""}`
Ha külön csatornára akarod a két detektort, ide írj chat ID-t. Üresen a közös `chatId`-re megy.

### `appLinkTemplate` — alap: `""`
Extra link az üzenet aljára, `{symbol}` helyettesítéssel — mobil app deep linkhez.
> **Példa:** `"bnc://app.binance.com/futures/{symbol}"`. Sima szövegként kerül bele,
> mert a Telegram Bot API csak http/https/tg sémát fogad el kattintható hivatkozásban.

---

# Recept: kevesebb, de kereskedhető jelzés

Egyszerre **csak egyet** állíts, hogy tudd, mi okozta a változást. Mind az
`app/config.py`-ban, utána `docker compose up -d --build`.

```python
# 1. csak a legforgalmasabb parok            MARKET_DEFAULTS
"minQuoteVolume24h": 300_000_000,
"maxSymbols": 50,

# 2. PUMP/DUMP: csak a rendkivuli mozgas     DETECTOR_DEFAULTS
"baselineRatio": 10.0,
"minMovePct": 1.20,

# 3. PUMP/DUMP: csak az, ami 2 percig VEGIG all
"confirmSec": 120.0,
"confirmHoldPct": 90,

# 4. REVERSAL: csak nagy mozgas utan         REVERSAL_DEFAULTS
"minMovePct": 3.00,
"baselineRatio": 10.0,

# 5. REVERSAL: korai belepo, friss szelsoertek
"maxRetracementPct": 20,
"maxExtremeAgeSec": 5,

# 6. ritkabban ugyanarrol a parrol
"symbolCooldownSec": 900,      # DETECTOR_DEFAULTS
"cooldownSec": 1800,           # REVERSAL_DEFAULTS
```

Több jelzés kell? Ugyanezek lefelé: `baselineRatio` 4.0, kisebb `minMovePct`,
`confirmSec` 5.0, rövidebb cooldown.

**Mielőtt lazítasz vagy szigorítasz: nézd meg a mért eredményt.**
```js
// mi lett a jelzesekbol detektoronkent, +5 percnel?
db.signals.aggregate([
  {$match: {"outcome.m5": {$exists: true}}},
  {$group: {_id: "$detector", n: {$sum: 1},
            jo: {$sum: {$cond: [{$gt: ["$outcome.m5.pct", 0]}, 1, 0]}},
            atlag: {$avg: "$outcome.m5.pct"}}}
])
```

Csak nézni akarod, Telegram nélkül — `TELEGRAM_DEFAULTS`:
```python
"enabled": False,
```

Csak néhány páron tesztelni — `MARKET_DEFAULTS`:
```python
"symbolWhitelist": ["BTCUSDT", "ETHUSDT"],
```

---

# Mit mutat a STATUS sor

```
STATUS  60 par | 14,113 tick/60s | konyv: 752 par | 3 candidate, 2 jelzes, 1 kihagyva | Telegram: BE
   kizarva 2: tul szeles a spread: 2
   spread   p10 0.004%  p50 0.016%  p90 0.042%   kuszob 0.050%  -> 2 par felette
   normal kesz: 58/60 par | legkozelebb: SKRUSDT 0.283% (kell 0.80%, normalja 0.038%)
   OSSZESITES  (+ = a jelzes iranyaba ment az ar)
                            +1p     +5p    +15p
     pump_dump   13 jelzes
       atlag             -0.31%  -0.40%  +0.02%
       talalat              23%     46%     54%
   UTOLSO JELZESEK
     ido   par          tipus irany      +1p     +5p    +15p
     04:49 SOLUSDT      rev   SHORT   +0.98%  +2.24%  +5.49%
```

- **`normal kesz`** — hány párnak épült már fel a normálja. Amíg nem kész, az a pár nem jelezhet.
- **`legkozelebb`** — a mezőny legjobbja épp mennyire van a küszöbtől. Ha itt tartósan
  0.05%-os mozgások vannak 0.50% mellett, akkor a piac áll — nem a beállítás rossz.
- **`spread` percentilisek** — a küszöb ebből állítható adat alapján, nem vaktában.
- **`OSSZESITES`** — detektoronként (`pump_dump` / `reversal`) két sor: az **átlagos**
  változás és a **találati arány** mérési pontonként. Irányhelyesen: `+` = LONG után
  felfelé ment az ár, vagy SHORT után lefelé. `talalat 54%` = a lemért jelzések
  54%-a ment jó irányba.
- **`UTOLSO JELZESEK`** — táblázat: mikor, melyik páron, melyik detektor (`pump` /
  `rev`), milyen irányba jelzett, és merre indult el az ár. `...` = az a mérési
  pont még nem járt le. **Ez az egyetlen visszajelzés arról, hogy a
  beállításaid működnek-e** — a többi szám csak azt mutatja, mit csinál a rendszer.

## Ezt látod egy jelzés útján a logban

```
MOZGAS      SOLUSDT  LONG  ar 184.21  +0.62% / 1.9s  normal 0.041% (15.1x)  -- 10 mp megerositesre var
CANDIDATE   SOLUSDT  LONG  ar 184.33  mozgas +0.62% / 1.9s  normal 0.041% (15.1x)  megtartott 118%
SIGNAL      SOLUSDT  LONG  ar 184.33  move +0.62% / 1.9s   https://www.binance.com/en/futures/SOLUSDT
```

vagy amikor nem lesz belőle jelzés:

```
MOZGAS      ZKCUSDT  SHORT ar 0.2841  -0.55% / 2.0s  normal 0.049% (11.2x)  -- 10 mp megerositesre var
VISSZAESETT ZKCUSDT  SHORT ar 0.2852  a -0.55%-bol -0.09% maradt (16%, kell 70%) -- pillanatnyi korrekcio volt
KIHAGYVA    BTRUSDT  ar 0.0912  a mozgas 78%-at EGYETLEN arlepes adta (max 40%)
REJECTED    CYSUSDT  LONG  ar 0.7845  tul szeles a spread
```

## Elutasítási okok

Gépi név megy a MongoDB-be (hogy aggregálható legyen), magyar szöveg a logba.

| ok | mit jelent |
|---|---|
| `blacklisted` / `not_whitelisted` | kézi kizárás a `market` dokumentumban |
| `no_book_data` | még nem láttuk a pár order book tetejét |
| `spread_too_wide` | a spread szélesebb, mint `maxSpreadPct` |
