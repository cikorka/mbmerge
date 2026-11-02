# Verzování a publikování mbmerge

[English](RELEASING.md) | **Čeština**

Balíček, repozitář i příkaz se jmenují **mbmerge**. GitHub Packages může zůstat prázdné: Python distribuce patří na PyPI, GitHub Releases označuje zdrojový snapshot a obsahuje poznámky k vydání.

## Jedna verze, tři vstupní body

Jediným zdrojem verze je `VERSION` v [merge_mbtiles.py](merge_mbtiles.py). Setuptools načte stejnou hodnotu do wheelu a zdrojového archivu, CLI ji vypíše a `mbmerge.__version__` ji zpřístupní v Pythonu. Nedopisuj další kopii do pyproject.toml. Databázové `SCHEMA_VERSION` je nezávislé číslo s pravidly z [ARCHITECTURE.cs.md](ARCHITECTURE.cs.md).

Tento checkout připravuje `2.0.0`, zásadní změnu proti slučovači dodaných zoomů verze 1.x. Používej tagy `v2.0.0`, `v2.0.1` atd. Publikované vydání ani jeho tag nepřepisuj. Změny vydávej pod novým číslem. Úprava checkoutu ani push větve samy release nevytvářejí.

- Patch, například `2.0.1`: kompatibilní opravy.
- Minor, například `2.1.0`: kompatibilní nové funkce.
- Major, například `3.0.0` po verzi 2: nekompatibilní změny veřejného CLI či datového kontraktu.

Python prerelease má tvar například `2.1.0rc1`, odpovídající tag je `v2.1.0rc1`. Na GitHubu ho označ jako prerelease. Produkční workflow prerelease přeskočí a odmítne jiný tvar než X.Y.Z, i kdyby byl náhled omylem označen jako stabilní. Zkušební publikování lze provést na odděleném TestPyPI.

## Připravené workflow

| Soubor | Spuštění | Úloha |
|---|---|---|
| [.github/workflows/ci.yml](.github/workflows/ci.yml) | Push, pull request, znovupoužitelné workflow | Testy na Linuxu/macOS s Pythonem 3.11–3.14; sestavení a ověření wheelu/sdistu, instalace přesného wheelu, test mimo zdrojovou složku, nové sestavení/instalace ze sdistu a uložení ověřených artefaktů. |
| [.github/workflows/testpypi.yml](.github/workflows/testpypi.yml) | Ruční Run workflow | Stejné kontroly, potom publikace na TestPyPI přes prostředí `testpypi`. |
| [.github/workflows/release.yml](.github/workflows/release.yml) | Zveřejněný stabilní GitHub Release | Stejné kontroly včetně přesné shody tagu `v<VERSION>`, potom publikace na PyPI přes prostředí `pypi`. |

Build a testy mají pouze čtení repozitáře. Jen publikační joby dostávají `id-token: write`. Publikační job nestahuje zdrojový checkout ani nic nesestavuje: převezme ověřené artefakty ze svého běhu až po úspěchu všech kontrol. V repozitáři není uložen API token ani heslo. Samotný push tagu na PyPI nepublikuje.

## Co se sestavuje a proč CI používá Node.js

Výsledkem je Python wheel (`py3-none-any`) a zdrojový archiv, nikoli samostatný nativní executable. Instalace vytvoří příkaz `mbmerge` v `bin`, který používá Python a Pillow. Matice Linux/macOS ověřuje kompatibilitu Pythonu, SQLite, zamykání souborů a obrazových knihoven; nekompiluje binárky pro tyto systémy.

GitHub Actions používá Node.js interně pro akce checkout, setup-python a přenos artefaktů. Přímo zvolené JavaScriptové akce používají Node.js 24; artefakty přenášejí `upload-artifact@v7.0.2` a `download-artifact@v8.0.2`. Node.js není závislost instalovaného mbmerge a do wheelu se nepřidává.

## Soukromé ověřování před publikací

Repozitář je [cikorka/mbmerge](https://github.com/cikorka/mbmerge). Dokud je soukromý, publikační joby PyPI i TestPyPI se přeskočí. CI, sestavení a instalace wheelu běží dál a artefakty zůstávají v soukromém repozitáři. Ověř skutečné vstupy a přínos projektu ještě před zveřejněním.

Po samostatném rozhodnutí vlastníka změnit viditelnost na veřejnou nastav v Settings → Secrets and variables → Actions → Variables podle potřeby `TESTPYPI_PUBLISH_ENABLED=true` a později `PYPI_PUBLISH_ENABLED=true`. Bez příslušné proměnné se daná publikace také přeskočí. Proměnné nejsou API tokeny. Založení release samo o sobě soukromý projekt nepublikuje na veřejný balíčkový index.

## Jednorázové nastavení vlastníkem repozitáře

1. Vytvoř GitHub repozitář `cikorka/mbmerge` a nahraj soubory projektu. `.github/workflows/` musí být v kořeni repozitáře. Případně povol GitHub Actions.
2. Založ účty na PyPI i TestPyPI a dokonči jejich požadované nastavení účtu, e-mailu a 2FA. Jde o oddělené účty i názvy projektů. HTTP 404 název nerezervuje ani nezaručuje registraci; dostupnost potvrď při nastavení.
3. V GitHub Settings → Environments vytvoř `pypi` a `testpypi`. U produkčního `pypi` vyžaduj schválení správcem před deploymentem a omez deploymenty na release tagy. Reviewery nastav podle vlastnictví a organizace.
4. V [nastavení publikování PyPI](https://pypi.org/manage/account/publishing/) přidej **pending publisher** pro nový projekt:
   - Název projektu PyPI: `mbmerge`.
   - Owner: `cikorka`.
   - Repository: `mbmerge`.
   - Workflow filename: `release.yml`.
   - Environment: `pypi`.
5. V [nastavení TestPyPI](https://test.pypi.org/manage/account/publishing/) přidej samostatného pending publishera: projekt `mbmerge`, stejný owner/repozitář, workflow `testpypi.yml`, prostředí `testpypi`.
6. Skutečné odkazy na `cikorka/mbmerge` jsou již v `[project.urls]` v pyproject.toml. Popis pro PyPI je README.pypi.md bez nefunkčních relativních odkazů na obrázky; GitHub README ponechává obrázkový návod.

Projekt vznikne při první úspěšné důvěryhodné publikaci. Pending publisher název nerezervuje. Název, vlastník, repozitář, workflow i prostředí musí přesně odpovídat konfiguraci daného indexu. Publikace může čekat na schválení chráněného prostředí.

## První publikace: TestPyPI a potom PyPI

1. Nahraj repozitář a ověř úspěch **Tests and distributions**.
2. V Actions zvol **Publish manually to TestPyPI**, použij **Run workflow** nad požadovaným commitem/refem. Před uploadem se sestaví a ověří distribuce. Zkontroluj projekt na TestPyPI a instalaci.
   V novém virtuálním prostředí nejdřív nainstaluj závislosti z repozitáře a potom samotný projekt pouze z TestPyPI:

```bash
python -m pip install -r requirements.txt
python -m pip install --index-url https://test.pypi.org/simple/ --no-deps 'mbmerge==2.0.0'
mbmerge --version
```

3. Pokud měníš verzi, uprav `VERSION`, commitni finální soubory a pushni. Pro současnou verzi:

```bash
git tag -a v2.0.0 -m "mbmerge 2.0.0"
git push origin v2.0.0
```

4. Na GitHubu vytvoř a **zveřejni** Release pro existující tag. **Set as a pre-release** použij jen pro náhledy; produkční publikace je přeskočí. Napiš poznámky ke změnám a kompatibilitě. Workflow znovu ověří projekt, porovná tag s verzí distribucí a po schválení prostředí nahraje wheel/sdist na PyPI.
5. V čistém prostředí ověř instalaci z PyPI:

```bash
pipx install mbmerge
mbmerge --version
```

Pro existující instalaci použij `pipx upgrade mbmerge`. GitHub automaticky nabídne zdrojové archivy tagu; wheel a sdist najdeš také v artefaktu `python-distributions` daného workflow a na PyPI. Workflow nepřipojuje další binární soubory přímo ke GitHub Release.

TestPyPI a PyPI mají oddělené verze: test `2.0.0` neobsadí `2.0.0` na ostrém indexu. Již nahrané distribuční soubory nelze přepsat. Opakování úspěšné publikace proto může skončit chybou; neobcházej ji změněnými artefakty pod stejnou verzí. Chybný build nebo kontrolu před uploadem lze po opravě bezpečně zopakovat.

## Lokální ověření před vydáním

Použij Python 3.11+ ve virtuálním prostředí:

```bash
python -m pip install '.[release]'
python -m unittest discover -s tests -v
python -m build
python -m twine check --strict dist/*
python tools/check_release.py --dist-dir dist --tag v2.0.0
python -m pip install --force-reinstall --no-deps dist/*.whl
python -I tools/smoke_installed.py
```

Použij čistý checkout nebo nejdřív odstraň jen staré vygenerované soubory v `dist/`: kontrola vyžaduje právě jeden wheel a jeden sdist požadované verze. Ověření úplnosti zdrojové distribuce:

```bash
python -m pip wheel --no-deps dist/*.tar.gz --wheel-dir rebuilt
python -m pip install --force-reinstall --no-deps rebuilt/*.whl
python -I tools/smoke_installed.py
```

Smoke test používá nainstalovanou distribuci a CLI spouští z dočasného adresáře, takže chybějící soubory wheelu nezakryje import ze zdrojů. Ověří oba kodéry, skutečné workery, smíšené vstupy, přesné pixely, metadata, opakování importu a kontrolu výstupu. `check_release.py` odmítá nesoulad tagu/verze, neočekávaná data ve wheelu a databáze mimo čtyři jmenované demo vstupy v sdistu. Build artefakty Git ignoruje.

Standardní `python -m build` používá izolované build prostředí. `--no-isolation` slouží jen pro offline/lokální sestavení s již nainstalovanými deklarovanými požadavky backendu. Pillow zůstává závislostí a nekopíruje se do našeho balíčku.

## Podklady

[PyPA balíčkování](https://packaging.python.org/en/latest/tutorials/packaging-projects/), [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/), [publikování přes GitHub Actions](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/), [TestPyPI](https://packaging.python.org/en/latest/guides/using-testpypi/), [Semantic Versioning](https://semver.org/).

## Kompatibilita verze 2

Verze 2.0.0 mění nové výstupy na merge základního zoomu s následnou tvorbou přehledů a používá schéma 3. Výstupy 1.x přestav do nových archivů z původních vstupů. Explicitní `--mode overlay` zachovává workflow dodaných zoomů pro nové výstupy. Oba režimy vynucují bezztrátové kódování včetně jedinečných ztrátových WebP. Poznámky k vydání musí popsat změnu chování a nová pevná nastavení zoomů/režimu.

Povolovací proměnné musí být **Repository variables**, nikoli proměnné uvnitř prostředí `pypi`/`testpypi`: podmínka celého jobu se vyhodnocuje dříve, než jsou dostupné proměnné prostředí. Pokud se proto publikace přeskočila, přidej proměnnou repozitáře a spusť znovu všechny joby existujícího release; nepřepisuj tag ani nezvyšuj verzi, dokud nedošlo k uploadu.
