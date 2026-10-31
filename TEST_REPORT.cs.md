# Protokol ověření – 31. 10. 2026

[English](TEST_REPORT.md) | **Čeština**

`merge_mbtiles.py`, verze 1.1.0, byl ověřen na macOS ARM64, Python 3.12.14, SQLite 3.53.1, Pillow 12.3.0, libwebp 1.6.0. Jde o syntetické testy. Reálná vstupní sada o velikosti 1 TB ani úplný import tisíců zdrojových archivů nebyly otestovány; produkční nasazení musí předcházet pilot na vlastních datech.

## Automatické testy

Příkaz `python -m unittest discover -s tests -v`: **22 testů, OK**. Výstup a dobu posledního běhu obsahuje [TEST_RESULTS.txt](TEST_RESULTS.txt).

Ověřené scénáře:

1. Částečně průhledné překryvy, priorita pozdější vrstvy, průhledná místa, pixelová shoda s Pillow alpha_composite, nezměněné bajty nepřekrytých dlaždic, TMS row beze změny, bounds/center/zoomy/atribuce, inkrementální přidání a přeskočení hotových vstupů.
2. Fallback bounds z jižních TMS řádků, odmítnutí NaN bounds, konzervativní antimeridián.
3. Změna zdrojového obsahu při stejné délce a obnoveném mtime: SHA-256 odmítne import ještě před přidáním nové vrstvy; ověřuje i zdroj vynechaný z nového příkazu. Obnovení přesných původních bajtů umožní pokračování.
4. Poškozené WebP, jiné rozměry, nevalidní TMS klíč, chybějící unikátní index a explicitní XYZ: chyba a žádné nové pixely.
5. Bajtový rozpočet omezuje dávku i při malých vysoce komprimovaných vstupech. `peak_inflight <= 2`, `peak_batch_bytes <= 3 MiB` při testovacím nastavení.
6. Chyba workeru poté, co část SQL dávky už byla zapsána: rollback všech jejích pixelů i checkpointu.
7. Dlouhý externí WAL reader: checkpoint se neprovede, import zastaví po registraci zdroje; po zavření readeru pokračuje bez opakovaného compositingu.
8. Příliš velký BLOB se odmítá podle délky před načtením obsahu. Podvržené obří WebP rozměry se odmítnou před dekódováním.
9. Odmítnutí vstupu totožného s výstupem, nepřevzetí neřízené DB, vzájemné vyloučení dvou instancí CLI.
10. Odmítnutí vstupního WAL režimu.
11. Simulovaná nečekaná změna zdroje během importu: trvalý stav `tainted` a odmítnutí dalšího běhu.
12. `EXPLAIN QUERY PLAN`: keyset čtení používá index bez dočasného třídění, output join vyhledává v indexu výstupu.
13. Pořadí seznamu, abecední pořadí adresáře, prázdné zdroje.
14. Skutečné násilné ukončení skupiny procesů pomocí SIGKILL pro PNG i WebP výstup s PNG zdrojem, před commitem a po commitu, před checkpointem WAL: SQLite integrity_check OK. Před commitem evidovány 2 dlaždice, po commitu 4; obnovení dokončí aktivní zdroj před novou vrstvou. Oba výsledky jsou po resume **bajtově shodné** s nepřerušeným během. Opakovaný import hotového poloprůhledného zdroje výstup nezmění.

Test změny během importu je řízená injekce `SourceChanged`, nikoli závod s neřízeným přepisem SQLite souboru. SIGKILL scénáře skutečně ukončují procesy. Fyzický výpadek napájení ani chování konkrétního disku při fsync se netestovaly.

## PNG/WebP testy doplněné ve verzi 1.1

Osm dalších testů ověřuje:

1. PNG překryvy, zachování původních bajtů jedinečné PNG dlaždice, `metadata.format=png` a dekódování výstupu.
2. Smíšené WebP/PNG vstupy do obou výstupních formátů: přesné RGBA pixely, jednotný formát všech výsledných dlaždic, zachování bajtů tam, kde to jde, a opakování bez změny výsledku.
3. Rozpoznání signatur při chybějícím formátu v metadatech, paletovou průhlednost, odstíny šedi, šedá+alfa a RGB.
4. Rozměry PNG před `Image.open`, vadné CRC chunků, odmítnutí APNG, explicitní odmítnutí 16bitového PNG a nesoulad deklarace formátu.
5. Pevný formát výstupu a aktivní kompresní parametr při resume; formát PNG se bez přepínače načte z existujícího výstupu.
6. Migraci WebP evidence schématu 1 na 2 při přidání PNG zdroje; pokus o změnu formátu výstupu se odmítne.
7. Rezervu na největší výstup i pro jedinečné převáděné dlaždice, omezení úloh a bajtů dávky a maximální velikost zakódovaného výstupu.
8. Odmítnutí nesprávně zakódované dlaždice při kontrole výstupu.

Původní publikované CLI 1.0 bylo navíc ručně spuštěno nad PNG výstupem schématu 2 a správně ho odmítlo. Migrace mění verzi evidence; po úspěšném převodu používej verzi 1.1+.

## Zátěž a RAM

Verze 1.1.0 byla změřena příkazem `python tests/benchmark_memory.py --tiles 64 512 --work-dir <temporary-directory>` a znovu s `--output-format png`.

Oba vstupy obsahují 512 × 512px bezztrátové RGBA WebP s deterministickým náhodným barevným obsahem a průhledností, se všemi souřadnicemi v překryvu. PNG běhy tedy ověřují i převod formátu. Čtyři workery, nejvýše osm odeslaných úloh, 32 MiB rozpočet dávky, 4 MiB limit blobu a ostatní výchozí hodnoty. Každý běh začíná novým výstupem. Záhlaví sloupců určuje výstupní formát a počet dlaždic na zdroj.

| Metrika | WEBP · 64 | WEBP · 512 | PNG · 64 | PNG · 512 |
|---|---:|---:|---:|---:|
| Dlaždic na zdroj | 64 | 512 | 64 | 512 |
| Příspěvků obou zdrojů | 128 | 1024 | 128 | 1024 |
| Součet vstupních DB (B) | 101228544 | 809558016 | 101228544 | 809558016 |
| Výstupní DB (B) | 51412992 | 405577728 | 58437632 | 460922880 |
| Celkový čas včetně měření (s) | 4.14 | 33.246 | 5.087 | 30.231 |
| Špička součtu RSS (MiB) | 423.67 | 390.14 | 313.25 | 321.27 |
| Současných procesů | 6 | 6 | 6 | 6 |
| Maximum odeslaných úloh | 8 | 8 | 6 | 6 |
| Maximum rozpočtu dávky (B) | 33130860 | 33130860 | 29898804 | 29898804 |
| Potvrzených datových dávek | 17 | 128 | 24 | 189 |

RSS se vzorkuje přibližně po 100 ms pomocí `ps` jako součet hlavního procesu, workerů a resource trackeru. Sdílené stránky se mohou započíst vícekrát. Jde o praktické vzorkované měření, nikoli přesný součet soukromých alokací. Nezahrnuje cache filesystemu OS, může minout krátké špičky a není pevným limitem RSS. U větších běhů nevzrostla paměť úměrně osminásobku vstupních dat; kolísání běhu a vzorkování může způsobit i vyšší naměřenou špičku u menšího běhu. Surová data a verze prostředí jsou v [BENCHMARK.json](BENCHMARK.json). Propustnost závisí na datech, překryvech, disku, cache a kompresi; výsledky nepředpovídají čas pro 1 TB.

## Demo a obrázky v README

Z přibalených PNG vstupů v `demo/png/` byly vytvořeny dočasné PNG i WebP výstupy. Úspěšný `verify --full-sqlite-check --decode`: 1 dekódovaná výsledná dlaždice, 0 nedokončených zdrojů. Oba výstupy mají RGBA pixely přesně shodné s `demo/png/expected_overlap.png`; opakovaný běh přeskočí oba evidované zdroje. Výstup se nepublikuje, protože jeho evidence obsahuje místní absolutní cesty; podle [README](README.cs.md) si vytvoř vlastní.

[Obrázek skládání](docs/images/compositing.png) je vytvořen ze skutečných náhledů dema a očekávaného výsledku. [Schéma dlaždic](docs/images/tile-grid.png) ukazuje jedinečné i společné souřadnice: části se stejným `z/x/y` se složí do jedné dlaždice, různé souřadnice zůstanou samostatné. Obrázky lze znovu vytvořit pomocí `examples/create_readme_images.py`.

## Provozní hranice

Změněné dříve importované vrstvy se odmítají, nepřegenerovávají. Zdrojové soubory musí zůstat dostupné na stejných cestách. Bounds se neořezávají podle alfa kanálu. Pevný celkový limit RAM nastavuje provozní prostředí, CLI zajišťuje omezené datové dávky/fronty/cache/rozměry. Dokumentace obsahuje postup pilotu a nastavení s menší pamětí. Automatické testy jsou spustitelné z přibalených zdrojů bez reálných dat.

## Příprava balíčkování a GitHub Actions

Balíček, repozitář i příkaz se nově jmenují `mbmerge`. VERSION zůstává 1.1.0, chování merge se nemění. Všech 22 testů prošlo na Pythonu 3.12 s Pillow 12.3.0 a znovu s Pillow 13.0.0; oba běhy jsou v [TEST_RESULTS.txt](TEST_RESULTS.txt).

Wheel i sdist prošly `twine check --strict` a kontrolou identity/obsahu distribucí. Přímá instalace wheelu i izolované nové sestavení ze sdistu prošly smoke testem nainstalovaného CLI mimo checkout: skutečné workery, smíšené PNG/WebP, přesné pixely překryvu, jednotný formát, původní bajty jedinečných dlaždic ve shodném formátu, metadata, opakování a plná kontrola výstupu. Záměrně chybný tag i náhledová verze určená pro produkci byly odmítnuty. Kontrolní soubory představující místní výstupy a `.env` se do distribucí nedostaly.

YAML workflow a podmínky publikování byly ověřeny lokálně. CI má nastavené Linux/macOS joby s Pythonem 3.11–3.14; skutečné GitHub Actions ani publikování PyPI/TestPyPI/OIDC zde spuštěny nebyly. Vlastník musí nastavit repozitář, účty, prostředí a Trusted Publisher podle [RELEASING.cs.md](RELEASING.cs.md). Žádný release ani balíček nebyl nahrán.
