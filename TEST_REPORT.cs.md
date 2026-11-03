# Zpráva o ověření — 2026-11-01

[English](TEST_REPORT.md) | **Čeština**

Zpráva se týká **mbmerge 2.2.0 / schéma 3**, včetně sloučení základní úrovně a následné tvorby nižších zoomů. Místní prostředí: macOS ARM64, Python 3.12.14, SQLite 3.53.1, Pillow 12.3.0, libwebp 1.6.0. Test nainstalované distribuce používá také Pillow 13.0.0. Vstupy jsou syntetické; reálná 1TB kolekce nebyla otestována.

## Správnost a obnova

Celá místní sada prošla: **30 testů**. Výstup a délku obsahuje [TEST_RESULTS.txt](TEST_RESULTS.txt). Zahrnuje **16 skutečných SIGKILL scénářů**: čtyři pro zdrojové transakce overlay a dvanáct pro import, průběžné přehledy a poslední přehled pyramidy, před/po commitu, pro PNG i WebP.

Ověřené vlastnosti:

- Výchozí pyramida importuje skutečný společný nejvyšší zoom, ignoruje dodané přehledy a generuje rodiče až po základním importu. Asymetrické barvy kontrolují TMS orientaci. Chybějící potomci jsou průhlední, bounds se neroztáhnou na celé přehledové dlaždice.
- Střídavé neprůhledné červené/modré pixely z různých zdrojů se nejprve sloučí a potom zmenší do neprůhledné fialové. Test odhaluje nesprávné zmenšení zdrojů před jejich skládáním. Neviditelná zelená se nepřenese do červeného alfa okraje.
- Jedinečné ztrátové WebP se v obou režimech převede na bezztrátové VP8L se zachováním dekódovaných pixelů. Smíšené zdroje mají jednotný výstup. Verify odhalí zvenku vložené ztrátové WebP nebo chybný formát.
- Rozdílná maxima, chybějící základ a neplatný rozsah zoomů se bezpečně odmítnou. Prázdný odmítnutý plán lze opravit bez smazání výstupu. Režim/zoomy/formát po importu nelze tiše změnit. Staré schéma 1/2 se odmítne bez změny bajtů i při potvrzených změnách čekajících ve WAL; schéma se kontroluje read-only před otevřením pro zápis.
- Přidání přepočítá pouze dotčené předky. Nedotčené větve zachovají bloby; inkrementální výsledek se shoduje s novým sestavením. Opakovaný import hotových zdrojů nic nepřekreslí ani nevytvoří další rodiče.
- Integrační test 96 zdrojů se společným dodaným z=1 importuje pouze z=8. Počet rodičů odpovídá nezávislé množině předků, bajty a úlohy nepřekročí limity.
- Skutečné SIGKILL zachová integritu SQLite, správné kurzory a trvalé plánování. Verify odmítne nedokončený stav; resume dokončí aktivní vynechaný zdroj před novým. Obnovené bloby odpovídají nepřerušenému sestavení i po posledním commitu rodiče před finální změnou fáze.
- Původní bezpečnostní testy nadále ověřují pořadí/alfu/metadata, hash při obnovené velikosti/mtime, poškozené obrázky a rozměry před dekodérem, PNG, SQL rollback, blokovaný checkpoint, zámek writera, taint, pořadí vstupů a indexované plány. Test pozdní dávky nad 10 000 klíči měří skutečnou práci SQLite VM a brání opakovanému skenování předchozích základních dlaždic.

Simulace změny zdroje ověřuje reakci evidence, ne všechny možné souběžné raw-file zápisy. SIGKILL je skutečný. Fyzický výpadek napájení, vadná synchronizace disků a všechny limity OS/hardwaru testovány nebyly.

## Opakovaný import, prázdné dlaždice a `tile` (reálná data)

`tests/test_reimport.py` a `tests/test_tile.py` přidávají 19 testů; celá sada (49) prochází s Pythonem 3.11.2, SQLite 3.40.1, Pillow 10.4.0 a GDAL 3.11.4 ([TEST_RESULTS.txt](TEST_RESULTS.txt)) i s Pythonem 3.13 / Pillow 13.0.0 bez GDAL (12 testů s GDAL přeskočeno). Pokrývají změněný zdroj odmítnutý ve výchozím stavu a přes `--reimport changed` přeložený stejně jako při čisté přestavbě, opakované `--reimport always`, po kterém zůstane výstup s binární alfou bajtově stejný, zdokumentované hromadění částečné alfy, pořadí podle seznamu, kontrolu přes stat, která zachytí i úpravu s obnoveným mtime, vynechané prázdné dlaždice, SIGKILL během opakovaného importu poloprůhledného zdroje bez dvojího přeložení a v `tile` okraje, nodata barvy, ořez, binární alfu, pořadí vstupů, PDF a přijetí výstupu příkazem merge.

Reálné územní plány (115 dokumentů okresu Benešov, základní zoom 17, 512 px):

- `tile` s ořezem v cílové mřížce: překryv sousedních obcí 0 px (jednokrokový ořez při reprojekci: 9 917 px u pěti sousedů) a žádné mezery na společných hranicích (18 435 → 0; zbylých 105 leží uvnitř jednotlivých dokumentů).
- `tile` zapisuje základní dlaždice rovnou do MBTiles z pracovních procesů, které čtou warp po obdélnících řádek po řádku, místo dlaždicování přes warpované VRT: největší plán (31 000 × 28 000 px, uložený po řádcích) 126 → 30 s; dvanáct dokumentů 378 → 115 s se čtyřmi dokumenty souběžně. Základní dlaždice pěti sousedních plánů jsou pixelově shodné s předchozí cestou přes pásy; mezery i překryv beze změny. Proti přesnému warpu (`-et 0`) se pixely liší nejvýš o 1; `gdalwarp` rovnou do souboru byl rychlejší, ale u 8 % pixelů se lišil až o 82, proto se nepoužívá.
- Ortofoto, 12 JPEG listů 20 000 × 16 000 px (world file v S-JTSK, `--s-srs EPSG:5514`), z19 až z0, 14 workerů: cesta přes pásy 780 s, přímý zápis 536 s (6,7 GB), `--quality 85` 184 s (1,0 GB). Viditelné pixely z19 shodné s cestou přes pásy (z 78 milionů se neliší žádný). Paměť ve špičce asi 500 MB na worker.
- Úrovně pyramidy: přednásobené `reduce(2)` místo BOX zmenšení, jehož zaokrouhlení posouvalo každou úroveň o +0,5. O čtyři úrovně níž klesla průměrná odchylka od přesného průměru 16 × 16 z 2,3–3,0 na 0,8–1,2.
- Úrovně pyramidy stavěné v pracovních procesech `tile` (2.2.0): bezztrátové výstupy územního plánu (z17–z0) i 12 listů ortofota (z19–z12) pixelově shodné s 2.1.0 na všech zoomech. S `--quality 85` PSNR proti bezztrátové pyramidě z18 37,3 → 38,1 dB, z15 34,5 → 35,5 dB (z19 beze změny 40,1 dB); čas procesoru 1 994–2 014 s → 1 816–1 832 s user, po dvou bězích na vytíženém stroji.
- `--reimport always` všech pěti dokumentů, dvakrát: výstup bajtově shodný s prvním sloučením (3 457 dlaždic).
- `--verify-recorded stat` nad 115 evidovanými zdroji: 0,38 s.
- Opakovaný import tří dokumentů do WebP výstupu s 49 496 dlaždicemi, zatímco MapProxy servírovala tentýž soubor: 12 100 požadavků, 0 chyb, checkpoint nebyl zablokován.

## Mnoho souborů, 512px a paměť

Každý soubor má jedinečnou základní PNG dlaždici z=14 a dodanou dlaždici z=1 společnou všem. Výchozí pipeline musí z=1 ignorovat a vytvořit každého potřebného předka jednou. Čtyři workery, max-inflight 8, rozpočet dávky 32 MiB, maximum blobu 4 MiB a výchozí komprese. Každý běh začíná novým výstupem.

| Výstup | Zdroje | Základní dlaždice | Vytvoření rodiče | Čas (s) | RSS stromu (MiB) | Maximum úloh | Maximum bajtů dávky |
|---|---:|---:|---:|---:|---:|---:|---:|
| webp | 128 | 128 | 134 | 9.473 | 269.3 | 7 | 29361724 |
| webp | 7000 | 7000 | 2382 | 554.466 | 262.81 | 7 | 29363178 |
| png | 128 | 128 | 134 | 20.771 | 217.91 | 7 | 29391138 |
| png | 7000 | 7000 | 2382 | 451.234 | 267.59 | 7 | 29422904 |

Všechny čtyři běhy prošly plnou kontrolou SQLite a dekódováním všech dlaždic. Klíče odpovídaly nezávislé množině základů/předků. Příspěvků bylo právě tolik jako zdrojů, čekajících rodičů nula a fáze complete. Oba běhy se 7000 soubory vytvořily **jedinou dlaždici z=1**, místo skládání 7000 dodaných přehledů. Počet generovaných přehledů odpovídal množině unikátních předků.

Raw velikosti, verze, procesy a výsledky obsahuje [BENCHMARK.json](BENCHMARK.json). Obrazy jsou jednoduché a opakované, soubory malé; nejde o 1 TB složitých map. Testuje se plán tisíců souborů, evidence, commity, diskoví rodiče, skutečné 512px kodeky a společný nízký zoom. Časy nelze přepočítávat na TB provoz.

RSS se vzorkuje asi po 100 ms přes ps jako součet rodiče CLI, workerů a resource trackeru. Nezahrnuje měřicí skript, přípravu dat, závěrečnou verifikaci ani filesystem cache. Sdílené stránky mohou být započtené vícekrát a krátká špička může uniknout. Oba kodéry a některé vývojové kontroly na hostiteli běžely souběžně; časy nejsou izolované rychlostní porovnání. Každý strom CLI se měří samostatně. Nejde o tvrdý RSS limit.

První pokus ve workspace zastavila změna fingerprintu vstupu před dokončením. Nepočítá se jako úspěšné měření. Generátory nyní explicitně zavírají SQLite spojení (samotný context manager pouze commituje), finální běhy používají dočasný adresář. Přesná příčina původní události nebyla určena; integritní kontroly se neoslabovaly ani neobcházely. Chyba nyní uvádí, která stat pole se změnila.

## Distribuce a dokumentace

Wheel/sdist prošly strict Twine a kontrolou identity/obsahu/tagu v2.2.0. Chybný tag se odmítá. Přímý wheel i wheel znovu sestavený ze sdistu prošly izolovaným testem instalovaného CLI mimo checkout: skutečné workery, smíšené 512px vstupy, oba bezztrátové kodéry, celá pyramida, přesné základní pixely, TMS, metadata, replay a dekódování. Místně byly ověřeny EN/CS odkazy a obrázky README.

Výsledky GitHub-hosted CI jsou v Actions kontrolách větve/PR. Místní výsledky netvrdí dostupnost všech podporovaných runnerů na tomto stroji. Validace nevytváří release tag, GitHub Release ani upload na PyPI/TestPyPI.

## Provozní omezení

Historické zdroje zůstávají neměnné a dostupné na evidovaných cestách. Každé spuštění je znovu hashuje, pokud není zadáno `--verify-recorded stat`; první velkou sadu importuj jedním příkazem. Nahrazování/odebírání vrstev, Windows a samostatné executably zůstávají mimo rozsah; GDAL vstupy, reprojekce a polygonový ořez patří do `mbmerge tile`, ne do `merge`. Osmibitový výpočet zaokrouhluje; bezztrátový kodek neznamená bezztrátové zmenšování. Publikuj dokončené zavřené archivy, vyhni se dlouhým WAL čtenářům, používej lokální SQLite disk a absolutní limit paměti nastav OS. Před provozem ověř reprezentativní skutečná data.
