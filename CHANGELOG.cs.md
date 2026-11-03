# Historie změn

[English](CHANGELOG.md) | **Čeština**

## 2.2.0 — nevydáno

- `tile` staví první čtyři úrovně pod základem (`PYRAMID_DEPTH`) přímo v pracovních procesech, z pixelů, které právě zakódovaly, místo dekódování uložených dlaždic: pracovní obdélníky jsou zarovnané na bloky 16 × 16 dlaždic a pro každou úroveň se drží jedna rozpracovaná řada (asi 30 MB na proces). Bezztrátové výstupy jsou pixelově shodné s 2.1.0 na všech zoomech. S `--quality 85` jsou z18–z15 asi o 1 dB blíž bezztrátové pyramidě (bez opakované ztrátové komprese) a ortofoto z 12 listů spotřebovalo asi o 9 % méně času procesoru. Malé rastry s méně obdélníky, než je procesů, použijí menší hloubku, aby dál pracovaly všechny procesy.
- Klíče dlaždic přehledů se ukládají jako celá čísla (celé číslo z numpy by se uložilo jako BLOB).

## 2.1.0 — 2026-11-02

### Změny

- `mbmerge tile` zapisuje základní dlaždice rovnou do MBTiles. Pracovní procesy (`--threads`) čtou warp po obdélnících řádek po řádku (u úzkých rastrů po celé šířce, u širokých mozaik v pruzích po 16 dlaždicích), ořez vypálí do alfy v cílové mřížce a kódují; jediným zapisovatelem do SQLite je rodič. Na disku nevznikají dočasné pásy ani adresáře dlaždic. Základní dlaždice územních plánů jsou pixelově shodné s 2.0.0; ortofoto z 12 JPEG listů do z19 trvalo 536 s místo 780 s.
- `mbmerge tile` nově potřebuje i Python bindingy GDAL (`osgeo`) stejné verze jako příkazy GDAL a numpy.
- Úrovně pyramidy v `merge` i `tile` počítá přednásobené `reduce(2)` místo BOX zmenšení, jehož zaokrouhlení posouvalo každou úroveň o +0,5 (o čtyři zoomy níž asi 2–3 stupně, nyní 0,8–1,2). Nižší zoomy výstupů přestavěných verzí 2.1.0 se proto mírně liší od 2.0.0; základní dlaždice se nemění.

### Novinky

- `tile --quality Q`: ztrátové WebP pro snímky publikované tak, jak jsou (ortofoto: 184 s a 1,0 GB místo 536 s a 6,7 GB). Vrstvy, které se slučují, nech bezztrátové.
- `tile --s-srs SRS`: vstupy bez souřadnicového systému (world file jako `.jgw`) se bez něj odmítnou; GDAL jejich souřadnice S-JTSK dřív tiše četl jako metry Web Mercatoru.
- `tile` rozbalí paletové vstupy do RGBA a každých 30 s loguje průběh.

## 2.0.0 — 2026-11-02

### Nekompatibilní změny

- Nový výstup standardně používá `pyramid`: import společného nejvyššího zoomu, potom tvorba nižších úrovní ze společného detailu až po `--min-zoom` (výchozí 0).
- Automatický výběr vyžaduje shodná skutečná maxima neprázdných zdrojů. Explicitní `--base-zoom` musí být v každém přítomný. Ostatní dodané úrovně se v tomto režimu ignorují.
- Schéma 3 ukládá pevný režim/mřížku/resampling/bezztrátovost i trvalou práci přehledů. Výstupy 1.x se odmítnou bez změn; vytvoř nový výstup z původních zdrojů.
- `verify` vrací chybu při nedokončeném importu nebo čekající/nefinalizované pyramidě.
- `merge` neukládá zcela průhledné zdrojové dlaždice (dříve se ukládaly jako jedinečné). Původní chování vrátí `--keep-empty`.

### Novinky a opravy

- `--mode overlay` zachovává původní workflow skládání dodaných zoomů pro nové výstupy.
- Trvale evidovaní deduplikovaní rodiče; generování odspodu ze čtyř aktuálních potomků, správné TMS rozmístění a BOX průměr s přednásobenou alfou.
- Atomické potvrzení obrazu rodiče, odstranění čekajícího klíče, vložení předka a postupu. Obnova po chybě/přerušení v obou fázích.
- Inkrementální změny přepočítají pouze dotčené předky; starý přehled se znovu nepřekrývá novým.
- Veškeré výstupní kódování je bezztrátové. Jedinečné ztrátové WebP se překóduje místo kopie; `verify --decode` odhalí ztrátové WebP vložené zvenku.
- Společný backpressure workerů, kontrola délek před čtením blobů, omezené SQL dávky rodičů a diskový plán. Obě fáze mají jediného SQLite writera.
- `status` obsahuje fázi, čekající rodiče, počet zápisů přehledů a pevná nastavení. Metadata uvádějí režim/fázi a skutečné generované zoomy.
- Testy sloučení sousedních příspěvků před zmenšením, průhledných hran, TMS, společného zoomu, selektivního přepočtu a skutečný SIGKILL kolem commitů základních dlaždic i přehledů.
- Reprodukovatelná měření mnoha 512px vstupů, obrázková EN/CS dokumentace a test nainstalovaného wheelu s celou pyramidou.
- Publikační návod rozlišuje povolovací proměnné repozitáře od proměnných prostředí.
- `merge --reimport changed|always` znovu přeloží uvedené, už importované zdroje přes aktuální výstup (novou verzi téhož souboru, nebo i nezměněný) a přepočítá dotčené předky; obnova po pádu nikdy nepoužije potvrzenou dávku dvakrát. Pro zdroje s alfou 0/255, jejichž nová verze pokrývá starou, je výsledek stejný jako čistá přestavba a opakování ho nemění.
- `merge --verify-recorded stat` kontroluje evidované zdroje podle otisku stat a hashuje jen ty, jejichž stat se změnil; vhodné pro přidávání po jednom zdroji k velké historii.
- Nový příkaz `mbmerge tile` (vyžaduje příkazy GDAL ≥ 3.11): GeoTIFF/PDF/VRT/JPEG → ploché bezztrátové MBTiles pro `merge`. Rastrování PDF, okraje (`--collar`, jako `nearblack`) a přesné barvy (`--nodata`) převedené na alfu před převzorkováním, warp do mřížky zvoleného zoomu a ořez polygonem **ve výsledné mřížce**, takže sousedé oříznutí společnou hranicí nemají překryv ani mezeru; `--binary-alpha`, vynechání prázdných dlaždic, volitelné přehledy pro náhledy. Warp se jednou přečte do dočasného GeoTIFF v paralelních pásech (`--threads`), takže se zdroje uložené po řádcích nedekomprimují znovu pro každou dlaždici; `--webp-method` platí i pro dlaždice kódované GDAL.
- Minimální Pillow je 10.4 (testy procházejí s 10.4 i 13).

### Zachovaná omezení

Bez reprojekce v `merge`, odebírání zdrojů, Windows a sestavení samostatných executablů. Opakovaný import neodstraní pixely, které měla jen starší verze zdroje. Bez `--verify-recorded stat` se historické zdroje při každém spuštění znovu hashují; první kolekci importuj jedním příkazem. Syntetické testy nenahrazují ověření reálné 1TB sady.

## 1.1.0

Inkrementální skládání dodaných PNG/WebP zoomů, omezení workerů/paměti, jediný SQLite writer, trvalé zdrojové kurzory, kontrola změn, metadata, MIT licence a workflow balíčkování/vydávání.
