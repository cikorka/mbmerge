# Historie změn

[English](CHANGELOG.md) | **Čeština**

## Nevydáno

- `mbmerge balance --landcover RASTR` dá mozaice mapových listů snímkovaných v různých dnech jednu expozici a barvu. Každý list ukotví na plochách, které vypadají v kterýkoli letní den stejně (výchozí jsou lesy a zástavba z WorldCover): percentily těchto tříd převede na celostátní. Proužky podél hran listů porovná v uzlech po 250 jednotkách robustní ortogonální regresí, mezi lety jen na těchto plochách, takže pole po žních blok neposunou. Mřížky uzlů s logaritmem zisku a posunem pro každý kanál řeší pro všechny listy společně jako membránu: švy uzavřené, uvnitř listů hladké, tažené ke kotvám. `tile --balance` je použije v každém výstupním pixelu před `--grade` a uloží je do metadat i do kontraktu navázání. `grade --balance` měří a ukáže barevnou úpravu na vyrovnané mozaice (výchozí jsou svázané úrovně) a `compare --balance` vyrovná i referenci.
- `tile`: když je základní zoom hrubší než vstupy a mozaika nemá přehledy (GDAL odvodí implicitní jen tehdy, když to jde u všech vstupů; 16 316 listů českého ortofota žádné nedostane), postaví se mozaika v největším hrubším rozlišení o mocninu dvou, které není hrubší než cíl, a každý vstup se otevře v úrovni přehledu, kterou potřebuje (`OVERVIEW_LEVEL`; JPEG: škálování DCT), stejně jako by to udělaly implicitní přehledy GDAL. Dřív každý blok warpu 512 × 128 dekódoval listy znovu v plném rozlišení: z15 celé republiky dával 2 dlaždice za sekundu na 32 workerech. Ukládá se jako `read_factor` do metadat i do kontraktu navázání.
- `grade --linked-levels` bere společný černý a bílý bod z histogramu jasu, už ne z nejtmavšího a nejsvětlejšího kanálu (po korekcích vstupů je pár oříznutých pixelů jednoho kanálu posunulo na 0 a 251, takže se nic neroztáhlo).
- `--preview` u `grade` čte vstupy jednou pro každou stranu (vlevo vstupy, vpravo vyrovnané a upravené vstupy).

## 2.4.0 — 2026-11-07

- `tile --resume`: postup (`rows_done` v událostech `tiling` a v závěrečné `base`, která nově nese `rows_done`/`rows`) započítá řádky hotové před přerušením, takže ukazuje postup celého výstupu, ne jen aktuálního běhu.
- `mbmerge grade` změří jednu barevnou úpravu (úrovně po kanálech, gamma, sytost, vibrance) pro celou mozaiku ze všech vstupů čtených v 1/8 (99 listů ortofota: 13 s na 12 vláknech); `--preview` vyrobí výřezy před/po. `tile --grade` ji použije na každý pixel před kódováním — jedna funkce všude, takže bez švů mezi listy — a zapíše ji do metadat i kontraktu navázání; `compare --grade` upraví i referenci. Upravené ortofoto při q85: dlaždice asi o 45 % větší.

## 2.3.1 — 2026-11-06

- `tile --cutline`: řady dlaždic, kterých se polygon nedotýká, se už ze zdrojů nečtou (dekódovaly se, převzorkovaly a pak ořízly do průhledna). Obálka hranice republiky leží zhruba z poloviny mimo ni, takže celostátní běh trávil hodiny na řadách bez jediné dlaždice. Výstup je bajtově shodný; přerušený běh 2.3.0 lze navázat přes `--resume`.

## 2.3.0 — 2026-11-06

- `tile --resume` pokračuje v `<výstup>.tmp`, který zůstal po chybě či zabití procesu (commit po každé řadě dlaždic, WAL). Hotové pracovní obdélníky a úrovně pyramidy přeskočí; vstupy (cesta, velikost, mtime) a nastavení musí být stejné, `--threads` se smí lišit. Změřeno na ortofotu zabitém po 10 minutách: navázaný výstup je bajtově shodný s nepřerušeným během.
- `tile --cutline` omezí warp na průnik polygonu se vstupem. Hranice republiky nad jedním listem ortofota dřív procházela prázdné řádky celé země (odhad 27 h místo 30 min).
- `tile --quality` staví všechny úrovně pod úrovněmi procesů z bezztrátových kopií v pomocném souboru, nikdy z dekódovaných ztrátových dlaždic (z13 ortofota: +0,7 dB PSNR).
- `tile --worker-cache-mib` (výchozí 128, dřív pevně 256): asi o 30 % méně paměti na proces při stejné rychlosti, 32 procesů se vejde do 16 GB. Ortofoto z 99 listů do z19: 16 procesů 1187 s, 32 procesů 637 s.
- `tile` čte hlavičky prostých vstupů přímo v procesu a `--s-srs` dá jednou celé mozaice, když ho všechny vstupy potřebují jako jediné (bez PDF, palety, `--collar`, `--nodata`): příprava 16 316 listů ortofota trvala 13 s místo 38 min. Mozaika bere nejjemnější rozlišení vstupů (`-resolution highest`), ne průměr. JPEG s poškozeným blokem EXIF (GDAL hlásí chybu, pixely jsou v pořádku) už nezastaví kontrolu hlaviček ani pracovní procesy; čtení, které nevrátí data, ano.
- `mbmerge compare` porovná MBTiles s přímým renderem zdrojů (PSNR, 5. percentil, SSIM po zoomech; potřebuje scikit-image). `tools/benchmark_tile_workers.py` měří paměť a rychlost pracovních procesů v pevném okně.

## 2.2.0 — 2026-11-03

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
