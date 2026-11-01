# Historie změn

[English](CHANGELOG.md) | **Čeština**

## 2.0.0 — nevydáno

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
- Nový příkaz `mbmerge tile` (vyžaduje příkazy GDAL ≥ 3.11): GeoTIFF/PDF/VRT/JPEG → ploché bezztrátové MBTiles pro `merge`. Rastrování PDF, okraje (`--collar`, jako `nearblack`) a přesné barvy (`--nodata`) převedené na alfu před převzorkováním, warp do mřížky zvoleného zoomu a ořez polygonem **ve výsledné mřížce**, takže sousedé oříznutí společnou hranicí nemají překryv ani mezeru; `--binary-alpha`, vynechání prázdných dlaždic, volitelné přehledy pro náhledy.
- Minimální Pillow je 10.4 (testy procházejí s 10.4 i 13).

### Zachovaná omezení

Bez reprojekce v `merge`, odebírání zdrojů, Windows a sestavení samostatných executablů. Opakovaný import neodstraní pixely, které měla jen starší verze zdroje. Bez `--verify-recorded stat` se historické zdroje při každém spuštění znovu hashují; první kolekci importuj jedním příkazem. Syntetické testy nenahrazují ověření reálné 1TB sady.

## 1.1.0

Inkrementální skládání dodaných PNG/WebP zoomů, omezení workerů/paměti, jediný SQLite writer, trvalé zdrojové kurzory, kontrola změn, metadata, MIT licence a workflow balíčkování/vydávání.
