# mbmerge – popis balíčku pro PyPI

[English](README.pypi.md) | **Čeština**

Inkrementální bezztrátové rastrové MBTiles: **nejprve sloučit společný nejvyšší zoom, potom vytvořit nižší zoomy ze společného detailu**. PNG/WebP s alfou, omezené dávky, paralelní workery, jediný SQLite writer a obnova obou fází po pádu.

## Instalace

```bash
pipx install mbmerge
mbmerge --version
```

Nebo `python -m pip install mbmerge` ve virtuálním prostředí. Vyžaduje Linux/macOS, Python 3.11+, SQLite 3.35+ a Pillow (instaluje se jako závislost). Windows není podporován. Pro WebP je potřeba jeho podpora v Pillow.

## Použití

```bash
mbmerge merge --input-dir /data/inputs --output /data/mosaic.mbtiles

# Společný základní zoom, výstupní úrovně 5..16 jako PNG:
mbmerge merge --input-dir /data/inputs --output /data/mosaic-png.mbtiles \
  --base-zoom 16 --min-zoom 5 --output-format png

# Zachování dodané kresby všech zoomů bez generování pyramidy:
mbmerge merge --mode overlay --input-dir /data/inputs --output /data/overlay.mbtiles

mbmerge status --output /data/mosaic.mbtiles
mbmerge verify --output /data/mosaic.mbtiles --decode
```

Nový výstup standardně používá pyramidu a bezztrátové WebP. Automatika vyžaduje shodný skutečný nejvyšší zoom neprázdných vstupů; explicitní základ musí být ve všech přítomný. Importuje se jen vybraná úroveň, dodané přehledy se ignorují. Po sloučení v pořadí zdrojů vzniknou nižší zoomy z nejvýše čtyř aktuálních potomků, s BOX průměrem a přednásobenou alfou. Chybějící potomci jsou průhlední. Další import přepočítá jen dotčené předky.

Po přerušení zopakuj příkaz. Hotové nezměněné zdroje se přeskočí, nové přidají za dřívější vrstvy, aktivní zdroj se dokončí první. Čekající rodiče/postup se potvrzují atomicky s pixely. Publikuj pouze fázi complete; verify odmítne nedokončený/tainted výstup. Během aktualizace mohou čtenáři vidět nový detail se starými přehledy.

## Bezztrátové kódování

PNG i WebP výstup jsou bezztrátové. WebP používá `lossless=True, exact=True`; samotné quality=100 by nestačilo. Jedinečné shodné PNG/bezztrátové WebP mohou zachovat původní bajty. Ztrátové WebP se i bez překryvu bezztrátově překóduje; ztracené detaily se neobnoví. Skládání a zmenšování stále zahrnuje osmibitové zaokrouhlování a snížení prostorového rozlišení.

## Vstupy a provozní omezení

Neměnné offline vstupy musí mít ploché MBTiles a unikátní vzestupný BINARY index `(zoom_level,tile_column,tile_row)`. Musí odpovídat mřížka Web Mercator/TMS a rozměry čtvercových dlaždic (výchozí 512 px). Lze kombinovat statické WebP a PNG do 8 bitů na kanál. Animace, 16bitové PNG, JPEG, vektory, normalizované schéma a WAL zdroje se odmítají. `merge` neprovádí reprojekci. Takové vstupy z GeoTIFF/PDF/VRT/JPEG vyrobí `mbmerge tile` (vyžaduje příkazy GDAL ≥ 3.11) včetně ořezu polygonem, převodu nodata/okrajů na alfu a bezztrátového balení. `--reimport changed|always` znovu přeloží novou verzi importovaného zdroje; `--verify-recorded stat` ušetří opakované hashování velké nezměněné historie.

Verze 2 používá schéma 3 a standardně tvoří pyramidu. Výstupy 1.x odmítne bez změn; vytvoř nový výstup z originálních zdrojů. Cizí archivy lze použít jako vědomě zploštěné vstupy, nikoli převzít jako spravovaný výstup. Po začátku importu jsou režim, zoomy, formát, rozměry a aktivní komprese pevné.

Změněné historické zdroje se odmítají. Zachovej jejich evidované cesty; obnov původní bajty nebo přestav výstup. Každé spuštění hashuje celou historii, proto první kolekci importuj jedním seznamem/adresářem, nikoli spuštěním pro každý soubor.

Úlohy, bloby a rozměry jsou omezené; rodiče čekají na disku. Rozpočet dávky není tvrdý RSS limit. Pyramida potřebuje nejméně pětinásobek maximálního blobu pro čtyři potomky a rodiče. Používej lokální SQLite disk, neblokuj výstup dlouhými čtenářskými transakcemi a rezervuj místo pro výstup/WAL/indexy. Bounds vycházejí ze zdrojových rozsahů nebo základních dlaždic bez alfa ořezu. Reálná 1TB kolekce nebyla ověřena.

Volby zobrazí `mbmerge merge --help`; funguje i `python -m mbmerge`. Repozitář obsahuje EN/CS dokumentaci, syntetické demo, crash testy a reprodukovatelná měření paměti a mnoha vstupů.

## Licence

MIT. Copyright (c) 2026 mbmerge contributors. Závazné znění je v LICENSE.md.
