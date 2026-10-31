# Historie změn

[English](CHANGELOG.md) | **Čeština**

## 2.0.0 — nevydáno

### Nekompatibilní změny

- Nový výstup standardně používá `pyramid`: import společného nejvyššího zoomu, potom tvorba nižších úrovní ze společného detailu až po `--min-zoom` (výchozí 0).
- Automatický výběr vyžaduje shodná skutečná maxima neprázdných zdrojů. Explicitní `--base-zoom` musí být v každém přítomný. Ostatní dodané úrovně se v tomto režimu ignorují.
- Schéma 3 ukládá pevný režim/mřížku/resampling/bezztrátovost i trvalou práci přehledů. Výstupy 1.x se odmítnou bez změn; vytvoř nový výstup z původních zdrojů.
- `verify` vrací chybu při nedokončeném importu nebo čekající/nefinalizované pyramidě.

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

### Zachovaná omezení

Bez přímých GDAL vstupů, reprojekce, polygonového clipu, výměny/odebírání zdrojů, Windows a sestavení samostatných executablů. Historické zdroje zůstávají neměnné a při každém spuštění se znovu hashují. První kolekci importuj jedním příkazem. Syntetické testy nenahrazují ověření reálné 1TB sady.

## 1.1.0

Inkrementální skládání dodaných PNG/WebP zoomů, omezení workerů/paměti, jediný SQLite writer, trvalé zdrojové kurzory, kontrola změn, metadata, MIT licence a workflow balíčkování/vydávání.
