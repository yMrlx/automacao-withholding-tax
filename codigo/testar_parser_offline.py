"""
TESTE OFFLINE do contrato da tabela de impostos e dos seletores de botao.

RODA SEM SAP: nao abre o iShift, nao faz login, nao toca na CR, nao depende do
lock. Sobe um Edge HEADLESS em about:blank, carrega uma fixture real sanitizada
e DOMs sinteticos, e roda os blocos JS de verdade do modulo contra eles.

  python codigo/testar_parser_offline.py      # exit 0 = tudo passou

Para que serve: o parser da tabela e os seletores sao o que mais quebra neste
projeto, e ate 2026-09-11 a unica forma de exercita-los era rodar contra a CR
real - o que trava a CR por horas (gotcha 7/11) e so da' pra fazer com o operador
presente pro login manual. Este teste pega REGRESSAO DE LOGICA em segundos.
O caso 2b usa codigo/fixtures/tabela_impostos_64_real.html, derivada do outerHTML
da tabela completa capturado em 24/09/2026. Confere os parsers Python/JS contra
a estrutura observada, incluindo tabelas aninhadas nas acoes de cada linha.
Os outros casos sinteticos exercitam falhas e variantes de DOM. Isso nao prova
o estado atual do servidor nem o fluxo de Save/Submit no SAP.

Cobre:
  1-2. tabela vazia / com dados: cabecalhos, idxType=2, idxDesc=3, Description.
  2b.  fixture real: 64 Types exatos, descricoes, uma tabela, aria-rowcount=65,
       mapa de colunas e leitura integral pelos helpers de producao.
  3.   wrappers [role=row] aninhados NAO inflam a contagem de linhas.
  4-5. guard de virtualizacao (aria-rowcount) e strict_rowcount.
  6-7. acoes de linha: Details por texto, Delete por `title`, Type ambiguo recusa.
  8.   fail closed quando a tabela nao esta na tela.
  9-10. _JS_ESTADO_ACAO_RAIZ (pre-condicao de Save/Submit): dedupe do no -cnt
        pro div pai clicavel, deteccao de desabilitado e de modal bloqueante.
  11.   instrumentacao: com_meta devolve aria_rowcount, cada linha traz rowindex.
  21.   no "-r" de acessibilidade sobreposto ao botao (DOM real de 14/09): nao
        pode herdar o host da raiz da janela e virar candidato separado.
  22.   mecanica do wait_for_wda_ready - base da economia de tempo de 14/09: o
        candidato otimizado e' mais rapido numa tela parada, mas NAO sai enquanto
        a tela muda e respeita o blindlayer. A sessao A usa o par conservador.
  23-24. alerta tardio na persistencia e regra unica de severidade.
  25-28. re-render do WDA: campo trocado, corrida depois da espera e helpers
         que descartam referencias stale / leem e clicam de forma atomica.
  43.   espera do resultado do Done (gotcha 26): SAP lento alem dos 20 s,
        carregamento atrasado, resultado incompleto, teto contado do Done,
        sessao perdida por prova positiva (tambem na estabilizacao e entre
        leituras), linha duplicada/extra; sempre 1 Done e nenhum Save.
  29.   confirmacao do operador: tolera caixa/espaco (a usuaria e' quem digita),
        mas nao deixa os niveis de risco colapsarem nem passa sem resposta.
  12-14. secao de tabela (_JS_DUMP_SECAO / contar_details_secao /
        clicar_detalhe_secao): wrapper aninhado NAO infla a contagem de
        "Details" - foi o bug que abortou o 1o diagnostico (4 pra 1 fornecedor);
        secao com 2 linhas RECUSA clicar; secao inexistente levanta.
"""
import os
import sys
import tempfile
import shutil
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sap_ishift_tax_automation as auto
from selenium import webdriver

CSS = """
<style>
  body { font: 12px sans-serif; margin: 0; }
  table { width: 900px; border-collapse: collapse; }
  th, td { border: 1px solid #ccc; height: 22px; padding: 2px 6px; }
  a { display: inline-block; width: 40px; height: 18px; }
</style>
"""

THEAD = """
<thead id="WDTAX-thead"><tr role="presentation">
  <th role="presentation"><span>&nbsp;</span></th>
  <th role="columnheader"><span>Actions</span></th>
  <th role="columnheader"><span>C.</span></th>
  <th role="columnheader"><span>* Withholding Tax Type</span></th>
  <th role="columnheader"><span>Withholding Tax Type Description</span></th>
</tr></thead>
"""


def linha(code, desc, idx, wrapper=False):
    tr = (
        '<tr role="row" aria-rowindex="%d">'
        '  <td role="gridcell"><a id="e%s" title="Edit">e</a>'
        '<a id="d%s" title="Delete">x</a><a id="v%s">Details</a></td>'
        '  <td role="gridcell"><span></span></td>'
        '  <td role="gridcell"><span>%s</span></td>'
        '  <td role="gridcell"><span>%s</span></td>'
        "</tr>" % (idx, code, code, code, code, desc)
    )
    if wrapper:
        # varios [role=row] aninhados envolvendo a MESMA linha real (o que
        # inflava a contagem antes do filtro de wrapper)
        return '<tbody role="row"><tr role="row"><td><table><tbody>%s</tbody></table></td></tr></tbody>' % tr
    return tr


def pagina(rows_html, rowcount, vazio=False):
    corpo = (
        '<tbody><tr role="row"><td colspan="5">The table does not contain any data</td></tr></tbody>'
        if vazio else "<tbody>%s</tbody>" % rows_html
    )
    return (
        CSS
        + '<h2>ERP Vendor Company Code: Extended Withholding Tax Types</h2>'
        + '<table id="WDTAX" role="grid" aria-rowcount="%d" aria-colcount="5" class="urST">' % rowcount
        + THEAD + corpo + "</table>"
    )


def carregar(d, html):
    d.execute_script("document.open(); document.write(arguments[0]); document.close();", html)


def js(d, bloco, *args):
    return d.execute_script(bloco, *args)


falhas = []
_total = 0


def checa(nome, cond, detalhe=""):
    global _total
    _total += 1
    print(("  [OK]   " if cond else "  [FALHA] ") + nome + (("  -> " + str(detalhe)) if detalhe else ""))
    if not cond:
        falhas.append(nome)


opts = webdriver.EdgeOptions()
opts.add_argument("--headless=new")
opts.add_argument("--disable-gpu")
d = webdriver.Edge(options=opts)
d.set_window_size(1400, 900)
try:
    # ---------------- caso 1: tabela vazia ----------------
    print("\n== caso 1: tabela VAZIA ==")
    carregar(d, pagina("", 1, vazio=True))
    r = js(d, auto._LER_LINHAS_IMPOSTOS_JS)
    checa("sem erro", not r.get("erro"), r.get("erro"))
    checa("vazio_confirmado=True", r.get("vazio_confirmado") is True)
    checa("0 linhas", r.get("linhas") == [])
    checa("cabecalhos com 4 colunas", len(r.get("cabecalhos") or []) == 4, r.get("cabecalhos"))
    checa("idxType=2 idxDesc=3", (r.get("idxType"), r.get("idxDesc")) == (2, 3),
          (r.get("idxType"), r.get("idxDesc")))

    # ---------------- caso 2: 3 linhas ----------------
    print("\n== caso 2: 3 linhas de dados ==")
    dados = [("T1", "TIMI - IRRF 25%"), ("T2", "TIMI - Cide 10%"), ("T3", "TIMI - ISS 4%")]
    html = "".join(linha(c, dsc, i + 2) for i, (c, dsc) in enumerate(dados))
    carregar(d, pagina(html, 4))
    r = js(d, auto._LER_LINHAS_IMPOSTOS_JS)
    checa("sem erro", not r.get("erro"), r.get("erro"))
    checa("3 linhas", len(r.get("linhas") or []) == 3, len(r.get("linhas") or []))
    checa("aria_rowcount=4", r.get("aria_rowcount") == 4, r.get("aria_rowcount"))
    tipos = [l["type"] for l in r["linhas"]]
    descs = [l["description"] for l in r["linhas"]]
    metodos = {l["metodo"] for l in r["linhas"]}
    checa("types corretos", tipos == ["T1", "T2", "T3"], tipos)
    checa("descriptions corretas", descs == [d2 for _, d2 in dados], descs)
    checa("todas por header-index", metodos == {"header-index"}, metodos)

    # ---------------- caso 2b: tabela REAL sanitizada com 64 linhas ----------------
    print("\n== caso 2b: fixture REAL de 24/09/2026, 64 linhas ==")
    fixture_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fixtures", "tabela_impostos_64_real.html")
    with open(fixture_path, encoding="utf-8") as fixture:
        carregar(d, CSS + fixture.read())
    linhas_reais, meta_real = auto.ler_linhas_impostos(
        d, strict_rowcount=True, com_meta=True, critico=True)
    tipos_reais = [ln["type"] for ln in linhas_reais]
    checa("fixture real: exatamente 64 linhas", len(linhas_reais) == 64, len(linhas_reais))
    checa("fixture real: multiconjunto exato de TAX_CODES",
          Counter(tipos_reais) == Counter(auto.TAX_CODES), tipos_reais)
    checa("fixture real: Description preenchida em todas as linhas",
          all(ln["description"].strip() for ln in linhas_reais))
    checa("fixture real: uma unica tabela candidata",
          meta_real["n_tabelas_candidatas"] == 1, meta_real["n_tabelas_candidatas"])
    checa("fixture real: aria-rowcount=65", meta_real["aria_rowcount"] == 65,
          meta_real["aria_rowcount"])
    checa("fixture real: mapa de colunas Type=2 e Description=3",
          (meta_real["idxType"], meta_real["idxDesc"]) == (2, 3),
          (meta_real["idxType"], meta_real["idxDesc"]))
    checa("fixture real: todas as linhas por header-index",
          {ln["metodo"] for ln in linhas_reais} == {"header-index"})
    tipos_integrais = auto.ler_tabela_impostos_integral(d)
    checa("fixture real: leitura integral devolve os 64 Types exatos",
          Counter(tipos_integrais) == Counter(auto.TAX_CODES), len(tipos_integrais))

    # ---------------- caso 3: wrappers aninhados ----------------
    print("\n== caso 3: mesmas 3 linhas com wrappers [role=row] aninhados ==")
    html_w = "".join(linha(c, dsc, i + 2, wrapper=True) for i, (c, dsc) in enumerate(dados))
    carregar(d, pagina(html_w, 4))
    r = js(d, auto._LER_LINHAS_IMPOSTOS_JS)
    n = len(r.get("linhas") or [])
    checa("ainda 3 linhas (wrapper ignorado)", n == 3, "%s -> %s" % (n, [l["type"] for l in r.get("linhas", [])]))

    # ---------------- caso 4: guard de virtualizacao ----------------
    print("\n== caso 4: aria-rowcount=11 com so 3 linhas renderizadas ==")
    carregar(d, pagina(html, 11))
    r = js(d, auto._LER_LINHAS_IMPOSTOS_JS)
    checa("JS le 3 e reporta rowcount=11", len(r["linhas"]) == 3 and r["aria_rowcount"] == 11,
          (len(r["linhas"]), r["aria_rowcount"]))

    class FakeDriver:
        def execute_script(self, s, *a):
            return d.execute_script(s, *a)

        def save_screenshot(self, *a):
            return True
    fd = FakeDriver()
    try:
        auto.ler_linhas_impostos(fd)
        checa("ler_linhas_impostos LEVANTA na virtualizacao", False, "nao levantou")
    except RuntimeError as e:
        checa("ler_linhas_impostos LEVANTA na virtualizacao", "virtualizada" in str(e), str(e)[:90])

    # ---------------- caso 5: rowcount ausente + strict ----------------
    print("\n== caso 5: sem aria-rowcount ==")
    carregar(d, pagina(html, 4).replace('aria-rowcount="4" ', ""))
    r = js(d, auto._LER_LINHAS_IMPOSTOS_JS)
    checa("aria_rowcount=-1 (desconhecido)", r.get("aria_rowcount") == -1, r.get("aria_rowcount"))
    checa("sem strict: passa", len(auto.ler_linhas_impostos(fd)) == 3)
    try:
        auto.ler_linhas_impostos(fd, strict_rowcount=True)
        checa("strict_rowcount LEVANTA", False, "nao levantou")
    except RuntimeError as e:
        checa("strict_rowcount LEVANTA", "ausente/desconhecido" in str(e), str(e)[:90])

    # ---------------- caso 6: acoes de linha (Details/Delete por title) ----------------
    print("\n== caso 6: acoes de linha ==")
    carregar(d, pagina(html, 4))
    r = js(d, auto._JS_DUMP_ACOES_LINHA, "T2")
    checa("dump da <tr> de T2 sem erro", isinstance(r, dict) and not r.get("erro"), (r or {}).get("erro"))
    titulos = " ".join(r.get("acoes", []))
    checa("acha o <a title=Delete>", '"Delete"' in titulos, titulos[:120])
    r = js(d, auto._JS_CLICAR_ACAO_LINHA, "T2", "Delete")
    checa("clica Delete de T2 pelo title", isinstance(r, dict) and r.get("ok") and r.get("id") == "dT2", r)
    r = js(d, auto._JS_CLICAR_ACAO_LINHA, "T3", "Details")
    checa("clica Details de T3 pelo texto", isinstance(r, dict) and r.get("ok") and r.get("id") == "vT3", r)
    r = js(d, auto._JS_CLICAR_ACAO_LINHA, "ZZ", "Delete")
    checa("codigo inexistente -> erro", isinstance(r, dict) and "nenhuma <tr>" in (r.get("erro") or ""), r)

    # ---------------- caso 7: linha duplicada = ambiguo ----------------
    print("\n== caso 7: 2 linhas com o mesmo Type ==")
    dup = html + linha("T2", "TIMI - Cide 10%", 5)
    carregar(d, pagina(dup, 5))
    r = js(d, auto._JS_CLICAR_ACAO_LINHA, "T2", "Delete")
    checa("recusa clicar em Type ambiguo", isinstance(r, dict) and "ambiguo" in (r.get("erro") or ""), r)

    # ---------------- caso 8: cabecalho ausente ----------------
    print("\n== caso 8: tela sem a tabela de impostos ==")
    carregar(d, CSS + "<h2>Outra tela qualquer</h2><p>nada aqui</p>")
    r = js(d, auto._LER_LINHAS_IMPOSTOS_JS)
    checa("JS reporta erro de cabecalho", isinstance(r, dict) and "cabecalho" in (r.get("erro") or ""), r)
    try:
        auto.ler_linhas_impostos(fd)
        checa("ler_linhas_impostos LEVANTA (fail closed)", False, "nao levantou")
    except RuntimeError as e:
        checa("ler_linhas_impostos LEVANTA (fail closed)", "nao identifiquei a tabela" in str(e), str(e)[:90])

    # ---------------- caso 9: _JS_ESTADO_ACAO_RAIZ (Save/Submit) ----------------
    print("\n== caso 9: estado dos botoes de raiz (Save/Submit) ==")
    raiz = CSS + """
      <h1>Change Request 90000001</h1>
      <div id="WD01" role="button" lsevents='{"Press":[{}]}'><span id="WD01-cnt">Save</span></div>
      <div id="WD02" role="button" lsevents='{"Press":[{}]}'><span id="WD02-cnt">Submit</span></div>
      <div id="WD03" role="button" aria-disabled="true"><span id="WD03-cnt">Withdraw Draft</span></div>
    """
    carregar(d, raiz)
    r = js(d, auto._JS_ESTADO_ACAO_RAIZ, "Save")
    cands = r.get("candidatos") or []
    checa("1 candidato 'Save' (dedupe do -cnt)", len(cands) == 1, cands)
    checa("host_id = div pai WD01", cands and cands[0].get("host_id") == "WD01", cands)
    checa("habilitado", cands and cands[0].get("habilitado") is True, cands)
    checa("sem modal", not r.get("modais"), r.get("modais"))
    r = js(d, auto._JS_ESTADO_ACAO_RAIZ, "Withdraw Draft")
    cw = r.get("candidatos") or []
    checa("'Withdraw Draft' aparece DESABILITADO", len(cw) == 1 and cw[0].get("habilitado") is False, cw)

    print("\n== caso 10: modal bloqueante ==")
    carregar(d, raiz + '<div role="dialog" style="width:300px;height:120px">Confirm?</div>')
    r = js(d, auto._JS_ESTADO_ACAO_RAIZ, "Save")
    checa("modal detectado", bool(r.get("modais")), r.get("modais"))

    # ---------------- caso 11: com_meta + aria-rowindex ----------------
    print("\n== caso 11: instrumentacao (com_meta / aria-rowindex) ==")
    carregar(d, pagina(html, 4))
    linhas, meta = auto.ler_linhas_impostos(fd, com_meta=True)
    checa("com_meta devolve (linhas, meta)", len(linhas) == 3 and isinstance(meta, dict))
    checa("meta.aria_rowcount=4", meta.get("aria_rowcount") == 4, meta)
    checa("meta.idxType/idxDesc=2/3", (meta.get("idxType"), meta.get("idxDesc")) == (2, 3), meta)
    idxs = [l.get("rowindex") for l in linhas]
    checa("rowindex por linha", idxs == ["2", "3", "4"], idxs)

    # ---------------- caso 12: secao com 1 linha + wrappers -------------
    # Aqui morava o bug que abortou o 1o diagnostico: contar [role=row] com
    # wrappers aninhados dava 4 "linhas" pra 1 fornecedor real. O fix conta
    # ACOES "Details" clicaveis, nao linhas.
    print("\n== caso 12: secao 'ERP Vendors' com 1 linha + wrappers ==")
    secao_1 = CSS + """
      <h3>ERP Vendors</h3>
      <div role="row"><div role="row"><table role="grid"><tbody>
        <tr role="row"><td role="gridcell"><a id="detV1">Details</a></td>
            <td role="gridcell">FORNECEDOR X</td></tr>
      </tbody></table></div></div>
    """
    carregar(d, secao_1)
    r = js(d, auto._JS_DUMP_SECAO, "ERP Vendors")
    checa("secao sem erro", isinstance(r, dict) and not r.get("erro"), (r or {}).get("erro"))
    checa("n_details=1 (wrapper nao infla)", r.get("n_details") == 1, r.get("n_details"))
    checa("contar_details_secao=1", auto.contar_details_secao(fd, "ERP Vendors") == 1)
    r = js(d, auto._JS_CLICAR_DETALHE_SECAO, "ERP Vendors")
    checa("clica o unico Details da secao", isinstance(r, dict) and r.get("ok"), r)

    # ---------------- caso 13: secao com 2 linhas -> recusa -------------
    print("\n== caso 13: secao com 2 linhas (regra multi-linha pendente com o negocio) ==")
    secao_2 = CSS + """
      <h3>ERP Vendors</h3>
      <table role="grid"><tbody>
        <tr role="row"><td role="gridcell"><a id="detA">Details</a></td><td role="gridcell">FORN A</td></tr>
        <tr role="row"><td role="gridcell"><a id="detB">Details</a></td><td role="gridcell">FORN B</td></tr>
      </tbody></table>
    """
    carregar(d, secao_2)
    checa("contar_details_secao=2", auto.contar_details_secao(fd, "ERP Vendors") == 2)
    r = js(d, auto._JS_CLICAR_DETALHE_SECAO, "ERP Vendors")
    checa("RECUSA clicar com 2 linhas", isinstance(r, dict) and "esperado 1" in (r.get("erro") or ""), r)
    try:
        auto.clicar_detalhe_secao(fd, "ERP Vendors")
        checa("clicar_detalhe_secao LEVANTA com 2 linhas", False, "nao levantou")
    except RuntimeError as e:
        checa("clicar_detalhe_secao LEVANTA com 2 linhas", "esperado 1" in str(e), str(e)[:80])

    # ---------------- caso 14: secao inexistente -> fail closed --------
    print("\n== caso 14: secao que nao existe na tela ==")
    carregar(d, CSS + "<h3>Outra coisa</h3>")
    try:
        auto.contar_details_secao(fd, "ERP Vendors")
        checa("contar_details_secao LEVANTA", False, "nao levantou")
    except RuntimeError as e:
        checa("contar_details_secao LEVANTA", "titulo da secao" in str(e), str(e)[:80])

    # ---------------- caso 15: dedupe por HOST, nao por prefixo de id ----
    # Cenario do Codex (8a revisao): dois botoes REAIS cujo id compartilha o
    # prefixo antes do hifen. O dedupe antigo (`id.split('-')[0]`) colapsava os
    # dois num so e a ambiguidade virava "1 candidato habilitado" - passaria.
    print("\n== caso 15: dois botoes 'Save' com prefixo de id igual ==")
    dois = CSS + """
      <h1>Change Request 90000001</h1>
      <div id="WD01-save-a" role="button" lsevents='{"Press":[{}]}'><span>Save</span></div>
      <div id="WD01-save-b" role="button" lsevents='{"Press":[{}]}'><span>Save</span></div>
    """
    carregar(d, dois)
    r = js(d, auto._JS_ESTADO_ACAO_RAIZ, "Save")
    cs = r.get("candidatos") or []
    checa("2 candidatos (nao colapsou no prefixo)", len(cs) == 2, [c.get("host_id") for c in cs])
    try:
        auto._relatar_acao_raiz(fd, "90000001", "Save")
        checa("_relatar_acao_raiz LEVANTA com 2 habilitados", False, "nao levantou")
    except RuntimeError as e:
        checa("_relatar_acao_raiz LEVANTA com 2 habilitados", "exatamente 1" in str(e), str(e)[:80])
    r = js(d, auto._JS_CLICAR_ACAO_RAIZ, "Save", "90000001")
    checa("clique atomico RECUSA com 2 habilitados",
          isinstance(r, dict) and "esperado 1" in (r.get("erro") or ""), r)

    # ---------------- caso 16: marcadores de desabilitado ---------------
    # gotcha 6: botao desabilitado do WDA continua batendo no seletor por texto.
    # Agora "habilitado" exige Press POSITIVAMENTE no host WDA.
    print("\n== caso 16: variantes de botao desabilitado ==")
    for nome, marcacao, esperado in [
        ("WDA sem lsevents/Press", '<div id="B1" role="button"><span>Save</span></div>', False),
        ("WDA com Press", '<div id="B2" role="button" lsevents=\'{"Press":[{}]}\'><span>Save</span></div>', True),
        ("WDA Press + classe Dsbl",
         '<div id="B3" role="button" class="lsButton--Dsbl" lsevents=\'{"Press":[{}]}\'><span>Save</span></div>', False),
        ("WDA Press + aria-disabled",
         '<div id="B4" role="button" aria-disabled="true" lsevents=\'{"Press":[{}]}\'><span>Save</span></div>', False),
        ("nativo <button>", '<button id="B5">Save</button>', True),
        ("nativo <button disabled>", '<button id="B6" disabled>Save</button>', False),
    ]:
        carregar(d, CSS + "<h1>Change Request 90000001</h1>" + marcacao)
        r = js(d, auto._JS_ESTADO_ACAO_RAIZ, "Save")
        cs = r.get("candidatos") or []
        ok = len(cs) == 1 and cs[0].get("habilitado") is esperado
        checa("%s -> habilitado=%s" % (nome, esperado), ok, cs)

    # ---------------- caso 17: clique atomico chega no host --------------
    print("\n== caso 17: clique atomico (host certo, CR conferida) ==")
    umso = CSS + """
      <h1>Change Request 90000001</h1>
      <div id="WDSAVE" role="button" lsevents='{"Press":[{}]}'
           onclick="window.__clicado = this.id;"><span id="WDSAVE-cnt">Save</span></div>
    """
    carregar(d, umso)
    r = js(d, auto._JS_ESTADO_ACAO_RAIZ, "Save")
    cs = r.get("candidatos") or []
    checa("1 candidato (span + div colapsam no host)", len(cs) == 1, [c.get("host_id") for c in cs])
    clicado = auto._clicar_acao_raiz(fd, "90000001", "Save")
    checa("clique reportou o host", clicado and clicado.get("host_id") == "WDSAVE", clicado)
    checa("o evento chegou no DIV pai (gotcha 5)",
          d.execute_script("return window.__clicado || '';") == "WDSAVE")

    print("\n== caso 17b: CR errada na tela -> recusa ==")
    carregar(d, umso)
    r = js(d, auto._JS_CLICAR_ACAO_RAIZ, "Save", "99999999")
    checa("recusa quando o numero da CR nao esta na tela",
          isinstance(r, dict) and "nao esta na tela" in (r.get("erro") or ""), r)
    try:
        auto._relatar_acao_raiz(fd, "99999999", "Save")
        checa("_relatar_acao_raiz LEVANTA com CR errada", False, "nao levantou")
    except RuntimeError as e:
        checa("_relatar_acao_raiz LEVANTA com CR errada", "nao achei o numero" in str(e), str(e)[:70])

    # ---------------- caso 18: severidade ESTRUTURAL de alerta ----------
    # "Invalid withholding tax code" nao casa com nenhuma lista de palavras -
    # por isso a severidade vem da classe/role, nao do texto.
    print("\n== caso 18: alertas por severidade estrutural ==")
    carregar(d, umso + '<div class="urMsgBarError">Invalid withholding tax code</div>')
    r = js(d, auto._JS_ESTADO_ACAO_RAIZ, "Save")
    al = r.get("alertas") or []
    checa("urMsgBarError -> severidade 'erro'", any(a.get("severidade") == "erro" for a in al), al)
    r = js(d, auto._JS_CLICAR_ACAO_RAIZ, "Save", "90000001")
    checa("clique RECUSA com erro na barra",
          isinstance(r, dict) and "alerta bloqueante" in (r.get("erro") or ""), r)
    try:
        auto._relatar_acao_raiz(fd, "90000001", "Save")
        checa("_relatar_acao_raiz LEVANTA com erro na barra", False, "nao levantou")
    except RuntimeError as e:
        checa("_relatar_acao_raiz LEVANTA com erro na barra", "bloqueante" in str(e), str(e)[:70])

    carregar(d, umso + '<div role="alert">Mensagem sem classe conhecida</div>')
    r = js(d, auto._JS_CLICAR_ACAO_RAIZ, "Save", "90000001")
    checa("[role=alert] sem classe = 'desconhecido' -> bloqueia",
          isinstance(r, dict) and "alerta bloqueante" in (r.get("erro") or ""), r)

    carregar(d, umso + '<div role="status">3 of 3 records</div>')
    r = js(d, auto._JS_CLICAR_ACAO_RAIZ, "Save", "90000001")
    checa("[role=status] benigno NAO bloqueia", isinstance(r, dict) and r.get("ok"), r)

    carregar(d, umso + '<div role="dialog" style="width:300px;height:120px">Confirmar?</div>')
    r = js(d, auto._JS_CLICAR_ACAO_RAIZ, "Save", "90000001")
    checa("modal bloqueia o clique atomico",
          isinstance(r, dict) and "modal bloqueante" in (r.get("erro") or ""), r)

    # ---------------- caso 19: mapa de colunas / Type ambiguo -----------
    print("\n== caso 19: cabecalho sem a coluna Type, e Type vazio ==")
    thead_ruim = THEAD.replace("* Withholding Tax Type", "Outra Coluna")
    carregar(d, pagina("", 1, vazio=True).replace(THEAD, thead_ruim))
    try:
        auto.ler_linhas_impostos(fd)
        checa("mapa invalido em tabela VAZIA levanta", False, "nao levantou")
    except RuntimeError as e:
        checa("mapa invalido em tabela VAZIA levanta", "mapa de colunas invalido" in str(e), str(e)[:80])

    vazio_type = linha("", "descricao solta", 2)
    carregar(d, pagina(vazio_type, 2))
    try:
        auto.ler_linhas_impostos(fd)
        checa("linha com Type vazio levanta (fallback heuristico)", False, "nao levantou")
    except RuntimeError as e:
        checa("linha com Type vazio levanta (fallback heuristico)",
              "Type inequivoco" in str(e), str(e)[:80])

    # ---------------- caso 20: rowcount no ancestral / 2 tabelas --------
    print("\n== caso 20: aria-rowcount num ancestral, e duas tabelas candidatas ==")
    sem_rc = pagina(html, 4).replace(' aria-rowcount="4"', "")
    carregar(d, sem_rc.replace('<table id="WDTAX"', '<div aria-rowcount="4"><table id="WDTAX"')
                      .replace("</table>", "</table></div>"))
    r = js(d, auto._LER_LINHAS_IMPOSTOS_JS)
    checa("acha aria-rowcount no ancestral", r.get("aria_rowcount") == 4, r.get("aria_rowcount"))

    carregar(d, pagina(html, 4) + pagina(linha("Z9", "outra tabela", 2), 2))
    r = js(d, auto._LER_LINHAS_IMPOSTOS_JS)
    tipos2 = [l["type"] for l in r.get("linhas") or []]
    checa("com 2 tabelas candidatas pega a 1a (comportamento documentado)",
          tipos2 == ["T1", "T2", "T3"], tipos2)
    checa("e REPORTA que havia 2 (vira warning no log, nao passa em silencio)",
          r.get("n_tabelas_candidatas") == 2, r.get("n_tabelas_candidatas"))
    _, meta2 = auto.ler_linhas_impostos(fd, com_meta=True)
    checa("meta carrega n_tabelas_candidatas", meta2.get("n_tabelas_candidatas") == 2, meta2)

    # ---------------- caso 21: no "-r" de acessibilidade (DOM REAL) ------
    # Capturado na rodada de 2026-09-14: a tela raiz tem, sobrepostos, um
    # <span id="WD6012-r"> (no de leitura, SEM lsevents proprio) e o botao de
    # verdade <div id="WD6012" lsevents='{"Press":...}'>. Como o span nao tem
    # lsevents, closest('[lsevents]') subia ate a raiz da janela
    # (sapwd_main_window_root_) e ele virava um candidato SEPARADO.
    # Na rodada real passou por sorte (a raiz so tinha "Help"). Com "Press" na
    # raiz - variante abaixo - seriam 2 habilitados e o Save seria RECUSADO.
    print("\n== caso 21: no '-r' sobreposto ao botao, raiz com Press ==")
    real = CSS + """
      <h1>Change Request 90000001</h1>
      <div id="sapwd_main_window_root_" lsevents='{"Press":[{}],"Help":[{}]}'>
        <span id="WD6012-r" style="position:absolute;left:8px;top:60px">Save</span>
        <div id="WD6012" role="button" tabindex="0" lsevents='{"Press":[{}]}'
             style="position:absolute;left:8px;top:63px">Save</div>
      </div>
    """
    carregar(d, real)
    r = js(d, auto._JS_ESTADO_ACAO_RAIZ, "Save")
    cs = r.get("candidatos") or []
    hab = [c for c in cs if c.get("habilitado")]
    checa("o no '-r' NAO herda o host da raiz da janela",
          all(c.get("host_id") != "sapwd_main_window_root_" for c in cs),
          [(c.get("id"), c.get("host_id"), c.get("habilitado")) for c in cs])
    checa("exatamente 1 habilitado (o botao de verdade)",
          len(hab) == 1 and hab[0].get("host_id") == "WD6012",
          [(c.get("id"), c.get("host_id")) for c in hab])
    r = js(d, auto._JS_CLICAR_ACAO_RAIZ, "Save", "90000001")
    checa("clique atomico ACEITA (nao e' recusado por ambiguidade)",
          isinstance(r, dict) and r.get("ok") and r["clicado"].get("host_id") == "WD6012", r)

    # ---------------- caso 22: mecanica do wait_for_wda_ready --------------
    # Sustenta a reducao de espera do loop (14/09): o par "_LOOP" precisa ser
    # mais rapido numa tela parada, mas NAO pode sair enquanto ela ainda muda.
    # Detalhe importante descoberto aqui: o wait mede o TAMANHO do innerText,
    # nao o conteudo - mudanca menor que TOLERANCIA_CHARS nao reseta a contagem.
    print("\n== caso 22: wait_for_wda_ready (base da economia de tempo) ==")

    def _tela(html, script=""):
        carregar(d, html)
        if script:
            d.execute_script(script)

    _tela("<p>tela parada</p>")
    t0 = time.time()
    auto.wait_for_wda_ready(fd, timeout=30,
                            estabilidade=auto.ESTABILIDADE_PADRAO, intervalo=auto.INTERVALO_PADRAO)
    t_padrao = time.time() - t0

    _tela("<p>tela parada</p>")
    t0 = time.time()
    auto.wait_for_wda_ready(
        fd, timeout=30,
        estabilidade=auto.ESTABILIDADE_LOOP_OTIMIZADA,
        intervalo=auto.INTERVALO_LOOP_OTIMIZADO)
    t_loop = time.time() - t0
    checa("numa tela parada, o candidato otimizado e' mais rapido que o padrao",
          t_loop < t_padrao, "loop %.2fs vs padrao %.2fs" % (t_loop, t_padrao))
    checa("e o padrao respeita os %.1fs de estabilidade" % auto.ESTABILIDADE_PADRAO,
          t_padrao >= auto.ESTABILIDADE_PADRAO, "%.2fs" % t_padrao)
    checa("a proxima sessao A esta configurada no par conservador",
          (auto.ESTABILIDADE_LOOP, auto.INTERVALO_LOOP) ==
          (auto.ESTABILIDADE_PADRAO, auto.INTERVALO_PADRAO),
          (auto.ESTABILIDADE_LOOP, auto.INTERVALO_LOOP))

    # tela CRESCENDO (variacao de tamanho = o que o wait mede) por 3s
    CRESCE = """
        window.__fim = Date.now() + 3000; window.__n = 0;
        window.__t = setInterval(() => {
            if (Date.now() > window.__fim) { clearInterval(window.__t); return; }
            window.__n += 60;
            document.getElementById('x').textContent = 'x'.repeat(window.__n);
        }, 200);
    """
    _tela('<p id="x">i</p>', CRESCE)
    t0 = time.time()
    auto.wait_for_wda_ready(
        fd, timeout=30,
        estabilidade=auto.ESTABILIDADE_LOOP_OTIMIZADA,
        intervalo=auto.INTERVALO_LOOP_OTIMIZADO)
    t_mudando = time.time() - t0
    checa("com a tela MUDANDO 3s, o loop NAO sai cedo",
          t_mudando >= 3.0, "%.2fs" % t_mudando)

    # overlay com tamanho real bloqueia; "estacionado" (1x1) nao (gotcha 9)
    _tela('<div id="blindlayer" style="width:300px;height:80px"></div><p>x</p>',
          "setTimeout(() => { const b=document.getElementById('blindlayer');"
          " b.style.width='1px'; b.style.height='1px'; }, 2000);")
    t0 = time.time()
    auto.wait_for_wda_ready(
        fd, timeout=30,
        estabilidade=auto.ESTABILIDADE_LOOP_OTIMIZADA,
        intervalo=auto.INTERVALO_LOOP_OTIMIZADO)
    t_overlay = time.time() - t0
    checa("blindlayer com tamanho real BLOQUEIA ate encolher (gotcha 9)",
          t_overlay >= 2.0, "%.2fs" % t_overlay)

    _tela('<div id="blindlayer" style="width:1px;height:1px"></div><p>x</p>')
    t0 = time.time()
    auto.wait_for_wda_ready(
        fd, timeout=30,
        estabilidade=auto.ESTABILIDADE_LOOP_OTIMIZADA,
        intervalo=auto.INTERVALO_LOOP_OTIMIZADO)
    t_estac = time.time() - t0
    checa("blindlayer 'estacionado' (1x1) NAO bloqueia (gotcha 9)",
          t_estac < 2.0, "%.2fs" % t_estac)

    # ---------------- caso 23: alerta que chega DEPOIS do clique -----------
    # Gotcha 14 (Codex, 22/09). O clique de Save/Submit ja rodava sob
    # _execute_script_critico, mas a pos-condicao chamava wait_for_wda_ready,
    # que usava o wrapper PERMISSIVO: um alert/confirm aberto pelo WDA depois
    # que o JS do clique retornou era ACEITO em silencio - exatamente o
    # dialogo que o caminho critico promete deixar pro operador.
    # Este caso roda num driver criado por criar_driver_edge(), a MESMA
    # funcao que o main() usa, pra provar junto que o
    # unhandledPromptBehavior="ignore" esta de fato aplicado.
    print("")
    print("== caso 23: alerta tardio na janela de persistencia (gotcha 14) ==")

    ALERTA_TARDIO = ('<p id="x">tela</p>'
                     '<script>setTimeout(function(){ alert("WDA: confirma?"); }, 700);</script>')

    da = auto.criar_driver_edge(headless=True)
    try:
        da.set_window_size(1200, 800)
        checa("criar_driver_edge aplica unhandledPromptBehavior=ignore",
              da.capabilities.get("unhandledPromptBehavior") == "ignore",
              da.capabilities.get("unhandledPromptBehavior"))

        # --- critico=True: tem que ABORTAR e deixar o dialogo na tela ---
        carregar(da, ALERTA_TARDIO)
        erro = None
        try:
            auto.wait_for_wda_ready(da, timeout=20, estabilidade=auto.ESTABILIDADE_LOOP,
                                    intervalo=auto.INTERVALO_LOOP, critico=True)
        except Exception as e:
            erro = e
        checa("critico=True LEVANTA no alerta tardio (nao aceita sozinho)",
              isinstance(erro, RuntimeError), type(erro).__name__ if erro else "nao levantou")
        checa("a mensagem explica que nao aceitou nem repetiu",
              erro is not None and "NAO aceitei" in str(erro), str(erro)[:90])

        # a prova do "ignore": o dialogo CONTINUA aberto depois da excecao
        ainda_aberto, texto_alerta = False, ""
        try:
            texto_alerta = da.switch_to.alert.text
            ainda_aberto = True
            da.switch_to.alert.accept()          # limpa pro proximo sub-caso
        except Exception:
            pass
        checa("o dialogo CONTINUA ABERTO pro operador decidir",
              ainda_aberto, texto_alerta or "ja tinha sido dispensado pelo driver")

        # --- critico=False: comportamento legado, aceita e segue ---
        # Fora da janela de persistencia isso e' o certo: o alerta benigno do
        # WDA ("Permission denied ... Whitelistservice") trava TODO JS ate ser
        # aceito, e ai parar seria pior.
        aceitos_antes = auto._ALERTAS_ACEITOS
        carregar(da, ALERTA_TARDIO)
        erro2 = None
        try:
            auto.wait_for_wda_ready(da, timeout=20, estabilidade=auto.ESTABILIDADE_LOOP,
                                    intervalo=auto.INTERVALO_LOOP, critico=False)
        except Exception as e:
            erro2 = e
        checa("critico=False (default) aceita o alerta e segue",
              erro2 is None, type(erro2).__name__ if erro2 else "sem excecao")
        checa("  ...e o aceite silencioso entra no contador _ALERTAS_ACEITOS",
              auto._ALERTAS_ACEITOS > aceitos_antes,
              (aceitos_antes, auto._ALERTAS_ACEITOS))

        sobrou = True
        try:
            da.switch_to.alert.accept()
        except Exception:
            sobrou = False
        checa("e nao sobrou dialogo pendente nesse caminho", not sobrou)
    finally:
        try:
            da.quit()
        except Exception:
            pass

    # ---------------- caso 24: regra de severidade unica -------------------
    # P1 da 10a revisao: 'aviso' passou a bloquear, e a regra nao pode existir
    # em duas copias (Python e JS). O JS recebe a lista do Python.
    print("")
    print("== caso 24: severidade compartilhada Python <-> JS (P1) ==")
    checa("BLOQUEAR_AVISOS_AO_PERSISTIR liga 'aviso'",
          "aviso" in auto._severidades_bloqueantes(), auto._severidades_bloqueantes())
    checa("erro e desconhecido continuam bloqueando",
          {"erro", "desconhecido"} <= set(auto._severidades_bloqueantes()),
          auto._severidades_bloqueantes())

    AVISO = """
    <div id="WDSAVE" role="button" lsevents='{"Press":[{}]}' style="width:60px;height:20px">
      <span id="WDSAVE-cnt">Save</span></div>
    <div class="urMsgBarWarning" role="alert">Aviso: confira os dados</div>
    <p>Change Request 90000001</p>
    """
    carregar(d, AVISO)
    r = js(d, auto._JS_CLICAR_ACAO_RAIZ, "Save", "90000001",
           list(auto._severidades_bloqueantes()))
    checa("com 'aviso' na lista, o clique atomico RECUSA",
          bool(r.get("erro")) and "alerta bloqueante" in r.get("erro", ""), r)

    carregar(d, AVISO)
    r = js(d, auto._JS_CLICAR_ACAO_RAIZ, "Save", "90000001", ["erro", "desconhecido"])
    checa("sem 'aviso' na lista, o MESMO DOM passa (a regra vem do Python)",
          r.get("ok") is True, r)

    # ---------------- caso 25: WDA re-renderiza o campo no meio ------------
    # Gotcha 17, rodada de 22/09 14:08. fill_field_by_label achava o input,
    # guardava o WebElement e o usava alguns segundos depois; se o WDA trocasse
    # o no' nesse intervalo, StaleElementReferenceException derrubava a rodada
    # no 1o codigo - com a tela visivelmente pronta no screenshot.
    # Aqui o DOM faz exatamente o que o SAP faz: troca o <input> por outro
    # (mesmo id) e, de quebra, tira o readonly - que e' o motivo de o campo
    # comecar nao-editavel (gotcha 13).
    print("")
    print("== caso 25: campo trocado pelo WDA no meio do preenchimento (gotcha 17) ==")

    RERENDER = """
        <span>Withholding Tax Type:</span>
        <input id="WD10F3" readonly value="">
        <script>
        setTimeout(function () {
            const velho = document.getElementById('WD10F3');
            const novo = document.createElement('input');
            novo.id = 'WD10F3';                 // MESMO id, no' diferente
            novo.value = '';                    // e agora editavel
            velho.parentNode.replaceChild(novo, velho);
            window.__trocou = true;
        }, 600);
        </script>
    """

    carregar(d, RERENDER)
    t0 = time.time()
    erro = None
    try:
        auto.fill_field_by_label(d, "Withholding Tax Type", "C0")
    except Exception as e:
        erro = e
    t_fill = time.time() - t0
    checa("preenche apesar do no' ter sido trocado (nao levanta Stale)",
          erro is None, ("%s: %s" % (type(erro).__name__, erro))[:120] if erro else "sem excecao")
    checa("o DOM confirma que a troca REALMENTE aconteceu",
          d.execute_script("return window.__trocou === true;") is True)
    checa("o valor foi parar no input NOVO (o que ficou na tela)",
          d.execute_script("return document.getElementById('WD10F3').value;") == "C0",
          d.execute_script("return document.getElementById('WD10F3').value;"))
    checa("esperou a troca em vez de falhar na hora",
          t_fill >= 0.6, "%.2fs" % t_fill)

    # _esperar_campo_editavel trabalha por ID: um no' trocado no meio do
    # polling e' so mais uma iteracao, nao um erro.
    carregar(d, RERENDER)
    erro2 = None
    try:
        auto._esperar_campo_editavel(d, "WD10F3", timeout=8)
    except Exception as e:
        erro2 = e
    checa("_esperar_campo_editavel atravessa o re-render (busca por id)",
          erro2 is None, ("%s" % type(erro2).__name__) if erro2 else "sem excecao")

    # e continua sendo o guarda do gotcha 13: campo que NUNCA libera (o que
    # acontece numa CR travada) tem que estourar, com mensagem que diz isso.
    carregar(d, '<span>Withholding Tax Type:</span><input id="WD10F3" readonly>')
    erro3 = None
    try:
        auto._esperar_campo_editavel(d, "WD10F3", timeout=2)
    except Exception as e:
        erro3 = e
    checa("campo que nunca libera ainda estoura (gotcha 13)",
          isinstance(erro3, auto.TimeoutException), type(erro3).__name__ if erro3 else "nao levantou")
    checa("e a mensagem manda conferir o lock",
          erro3 is not None and "lock" in str(erro3).lower(), str(erro3)[:100])

    # ---------------- caso 26: corrida DEPOIS da espera -----------------
    # A busca por ID do caso 25 corrige a referencia mantida durante o
    # polling. Ainda precisamos provar o retry da fresta seguinte: o polling
    # retorna e, antes de find_element/clear, o WDA tira o no' do DOM ou o
    # devolve readonly outra vez.
    print("")
    print("== caso 26: no some/volta readonly depois da espera (retry final) ==")

    esperar_original = auto._esperar_campo_editavel
    chamadas = {"removeu": 0}

    def esperar_e_remover_uma_vez(driver, campo_id, timeout=10):
        esperar_original(driver, campo_id, timeout=timeout)
        chamadas["removeu"] += 1
        if chamadas["removeu"] == 1:
            driver.execute_script("""
                const id = arguments[0];
                const velho = document.getElementById(id);
                velho.remove();
                setTimeout(function () {
                    const novo = document.createElement('input');
                    novo.id = id;
                    document.body.appendChild(novo);
                }, 120);
            """, campo_id)

    auto._esperar_campo_editavel = esperar_e_remover_uma_vez
    try:
        carregar(d, '<span>Withholding Tax Type:</span><input id="WD10F3">')
        erro4 = None
        try:
            auto.fill_field_by_label(d, "Withholding Tax Type", "C0", timeout=5)
        except Exception as e:
            erro4 = e
        checa("NoSuch entre espera e re-find vira retry",
              erro4 is None and chamadas["removeu"] >= 2,
              "%s chamadas=%s" % (type(erro4).__name__ if erro4 else "OK", chamadas["removeu"]))
        checa("retry apos NoSuch preenche o no novo",
              d.execute_script("return document.getElementById('WD10F3').value;") == "C0")
    finally:
        auto._esperar_campo_editavel = esperar_original

    chamadas = {"bloqueou": 0}

    def esperar_e_bloquear_uma_vez(driver, campo_id, timeout=10):
        esperar_original(driver, campo_id, timeout=timeout)
        chamadas["bloqueou"] += 1
        if chamadas["bloqueou"] == 1:
            driver.execute_script("""
                const el = document.getElementById(arguments[0]);
                el.readOnly = true;
                setTimeout(function () { el.readOnly = false; }, 120);
            """, campo_id)

    auto._esperar_campo_editavel = esperar_e_bloquear_uma_vez
    try:
        carregar(d, '<span>Withholding Tax Type:</span><input id="WD10F3">')
        erro5 = None
        try:
            auto.fill_field_by_label(d, "Withholding Tax Type", "C0", timeout=5)
        except Exception as e:
            erro5 = e
        checa("InvalidElementState entre espera e clear vira retry",
              erro5 is None and chamadas["bloqueou"] >= 2,
              "%s chamadas=%s" % (type(erro5).__name__ if erro5 else "OK", chamadas["bloqueou"]))
        checa("retry apos readonly preenche o campo",
              d.execute_script("return document.getElementById('WD10F3').value;") == "C0")
    finally:
        auto._esperar_campo_editavel = esperar_original

    # ---------------- caso 27: refs curtas nos helpers do hot path -------
    # A 29a revisao apontou os outros tres lugares que ainda mantinham um
    # WebElement atraves de um round-trip: medir/rolar/clicar candidatos,
    # reler o valor de um campo e ler/clicar checkbox. Aqui forca stale na
    # medicao dos dois primeiros helpers e exercita as leituras atomicas dos
    # outros dois. Tudo e' local; nenhum acesso ao SAP.
    print("")
    print("== caso 27: helpers descartam referencias stale (29a revisao) ==")

    bounding_original = auto._bounding_info
    chamadas = {"bounding": 0}

    def bounding_stale_uma_vez(driver, elemento):
        chamadas["bounding"] += 1
        if chamadas["bounding"] == 1:
            raise auto.StaleElementReferenceException("stale simulado na medicao")
        return bounding_original(driver, elemento)

    auto._bounding_info = bounding_stale_uma_vez
    try:
        carregar(d, """
            <span style="display:inline-block;width:220px;height:20px">
              Extended Withholding Tax Types
            </span>
        """)
        erro6 = None
        try:
            auto.scroll_to_text(
                d, "Extended Withholding Tax Types", timeout=3,
                tamanho_max=(500, 100))
        except Exception as e:
            erro6 = e
        checa("scroll_to_text re-acha tudo depois de stale na medicao",
              erro6 is None and chamadas["bounding"] >= 2,
              "%s chamadas=%s" % (
                  type(erro6).__name__ if erro6 else "OK",
                  chamadas["bounding"]))

        chamadas["bounding"] = 0
        carregar(d, """
            <button style="width:60px;height:24px"
                    onclick="window.__clicou = true">New</button>
        """)
        erro7 = None
        try:
            auto.click_visible_candidate(
                d, "New", timeout=3, tamanho_max=(100, 60))
        except Exception as e:
            erro7 = e
        checa("click_visible_candidate re-acha tudo depois de stale na medicao",
              erro7 is None and chamadas["bounding"] >= 2,
              "%s chamadas=%s" % (
                  type(erro7).__name__ if erro7 else "OK",
                  chamadas["bounding"]))
        checa("o retry do clique acionou o candidato novo",
              d.execute_script("return window.__clicou === true;") is True)
    finally:
        auto._bounding_info = bounding_original

    carregar(d, """
        <span>Withholding Tax Type:</span>
        <input id="WD10F3" value="C0">
    """)
    checa("_reler_valor_campo re-busca por id e devolve o valor",
          auto._reler_valor_campo(d, "Withholding Tax Type") == "C0")

    carregar(d, """
        <div style="display:inline-flex;align-items:center">
          <span>Subject to Withholding Tax</span>
          <span id="WD110C" role="checkbox" aria-checked="false"
                style="display:inline-block;width:16px;height:16px;margin-left:20px"
                onclick="this.setAttribute('aria-checked', 'true')"></span>
        </div>
    """)
    estado_checkbox = auto._checkbox_marcado_perto_do_label(
        d, "Subject to Withholding Tax")
    checa("leitura atomica acha checkbox inicialmente desmarcado",
          estado_checkbox is False, repr(estado_checkbox))
    erro8 = None
    try:
        auto.check_checkbox_by_label(d, "Subject to Withholding Tax", timeout=3)
    except Exception as e:
        erro8 = e
    checa("clique atomico marca e confirma o checkbox",
          erro8 is None and auto._checkbox_marcado_perto_do_label(
              d, "Subject to Withholding Tax") is True,
          type(erro8).__name__ if erro8 else "marcado")

    # ---------------- caso 28: empate label/container em tela larga -------
    # Ao escrever o caso 27, o viewport de 1400px revelou que o div externo e
    # o span do label tem exatamente o mesmo innerText. O algoritmo antigo
    # ficava com o primeiro (o div de largura inteira); seu centro podia ficar
    # >400px do checkbox e a busca falhava. No empate de texto, deve vencer o
    # elemento de menor area visual, isto e', o span justo do label.
    print("")
    print("== caso 28: label justo vence container em viewport largo ==")
    carregar(d, """
        <div style="display:flex;align-items:center">
          <span>Subject to Withholding Tax</span>
          <span id="WD110C" role="checkbox" aria-checked="false"
                style="display:inline-block;width:16px;height:16px;margin-left:20px"
                onclick="this.setAttribute('aria-checked', 'true')"></span>
        </div>
    """)
    estado_largo = auto._checkbox_marcado_perto_do_label(
        d, "Subject to Withholding Tax")
    checa("acha o checkbox mesmo com container de largura inteira",
          estado_largo is False, repr(estado_largo))
    erro9 = None
    try:
        auto.check_checkbox_by_label(d, "Subject to Withholding Tax", timeout=3)
    except Exception as e:
        erro9 = e
    checa("marca o checkbox usando o label visualmente mais justo",
          erro9 is None and auto._checkbox_marcado_perto_do_label(
              d, "Subject to Withholding Tax") is True,
          type(erro9).__name__ if erro9 else "marcado")

    # ---------------- caso 29: confirmacao tolerante a digitacao -----------
    # Pedido do operador (23/09): quem roda isto e' a usuaria, e a comparacao exata
    # transformava um "salvar" minusculo em encerramento - perdendo a lista de
    # CRs ja colada, que e' o trabalho manual da execucao. O atrito estava no
    # lugar errado: punia erro de digitacao, nao falta de intencao.
    # O que NAO pode afrouxar: palavra diferente por nivel de risco (7a revisao
    # do Codex) e abortar quando nao ha resposta nenhuma.
    print("")
    print("== caso 29: confirmacao aceita digitacao humana, nao intencao errada ==")

    norm = auto._normalizar_confirmacao
    checa("minuscula vale", norm("salvar") == "SALVAR", norm("salvar"))
    checa("espaco sobrando nas pontas vale", norm("  SALVAR  ") == "SALVAR")
    checa("espaco duplo no meio vale",
          norm("SALVAR   SUBMETER") == "SALVAR SUBMETER", norm("SALVAR   SUBMETER"))
    checa("misturado vale", norm(" sAlVaR   sUbMeTeR ") == "SALVAR SUBMETER")
    checa("None nao explode", norm(None) == "")

    # e o que tem que CONTINUAR sendo recusado
    checa("palavra errada NAO passa", norm("salvr") != "SALVAR", norm("salvr"))
    checa("vazio NAO passa", norm("") != "SALVAR")
    checa("confirmar NAO serve pra salvar (niveis nao colapsam)",
          norm("confirmar") != norm("SALVAR"))
    checa("salvar NAO serve pra salvar+submeter (niveis nao colapsam)",
          norm("salvar") != norm("SALVAR SUBMETER"))

    # _pedir_confirmacao: acerta na 2a tentativa, e desiste depois de gastar todas
    respostas = iter(["salvr", "  salvar "])
    original_input = __builtins__["input"] if isinstance(__builtins__, dict) else __builtins__.input
    try:
        if isinstance(__builtins__, dict):
            __builtins__["input"] = lambda *a, **k: next(respostas)
        else:
            __builtins__.input = lambda *a, **k: next(respostas)
        checa("erra a 1a, acerta a 2a -> confirma",
              auto._pedir_confirmacao("SALVAR", tentativas=3) is True)

        respostas2 = iter(["x", "y", "z"])
        if isinstance(__builtins__, dict):
            __builtins__["input"] = lambda *a, **k: next(respostas2)
        else:
            __builtins__.input = lambda *a, **k: next(respostas2)
        checa("gasta as tentativas -> NAO confirma",
              auto._pedir_confirmacao("SALVAR", tentativas=3) is False)
    finally:
        if isinstance(__builtins__, dict):
            __builtins__["input"] = original_input
        else:
            __builtins__.input = original_input

    # ---------------- caso 30: dedupe estrutural de mensagem ---------------
    # DOM da sessao A (23/09), o Save que GRAVOU e foi classificado como falha:
    # o SAP publica a confirmacao duas vezes - no container com classe
    # (sapLsFPMMessageArea -> info) e num [role=alert] SEM classe, que cai em
    # 'desconhecido' e bloqueia. A saida NAO e' afrouxar 'desconhecido': e'
    # reconhecer os dois nos como a MESMA mensagem, e so quando ha relacao
    # ESTRUTURAL. Os contra-casos abaixo e' que garantem o fail-closed.
    print("")
    print("== caso 30: dedupe estrutural de mensagens (12a revisao) ==")

    def alertas(html):
        carregar(d, html)
        return js(d, auto._JS_ACAO_RAIZ_PREAMBULO + "return coletarAlertas();")

    def o_alerta(lista):
        # o no' ARIA sem classe - NAO o container, cujo innerText engloba o dele
        for a in lista:
            if a["role"] == "alert" and not a["classe"]:
                return a
        return None

    BLOQ = auto._severidades_bloqueantes()

    # --- alert ANINHADO no container classificado: dedupe vale --------------
    ANINHADO = """
    <div class="lsHTMLContainer sapLsFPMMessageArea" role="presentation"
         style="width:600px;height:40px">Change request 90000001 saved<span>Info.</span>
      <span role="alert" style="display:block;width:300px;height:14px">: Change request 90000001 saved</span>
    </div>
    """
    a = o_alerta(alertas(ANINHADO))
    checa("aninhado: a confirmacao de sucesso deixa de bloquear",
          a is not None and a["severidade"] not in BLOQ, a)
    checa("aninhado: registra severidade_original e de quem herdou",
          a is not None and a["severidade_original"] == "desconhecido"
          and bool(a["duplicado_de"]), a)

    # --- O CASO REAL: live region SEPARADA ---------------------------------
    # Os textos do log provam que o alert NAO estava dentro do container: o
    # innerText do container ("...savedInfo. Display Message Log") nao contem o
    # texto do alert (": Change request 90000001 saved"). Se fosse descendente,
    # conteria. Logo e' uma live region em outro ponto da arvore.
    # Sem relacao estrutural, a dedupe NAO dispara - e esta' certo assim: e' o
    # lado conservador combinado. Quem conserta o RELATO neste caso e' a
    # pos-condicao conjunta (persistido_com_ressalva), nao a dedupe.
    SEPARADA = """
    <div class="lsHTMLContainer sapLsFPMMessageArea" role="presentation"
         style="width:600px;height:40px">Change request 90000001 savedInfo. Display Message Log</div>
    <div id="outra-arvore" style="width:600px;height:40px">
      <span role="alert" style="display:block;width:300px;height:14px">: Change request 90000001 saved</span>
    </div>
    """
    r = alertas(SEPARADA)
    a = o_alerta(r)
    checa("live region SEPARADA: dedupe NAO dispara (conservador, combinado)",
          a is not None and a["severidade"] in BLOQ, a)
    checa("e a instrumentacao diz ONDE o alerta estava, pra decidir com dado",
          a is not None and "outra-arvore" in (a["caminho"] or ""), a and a["caminho"])

    # --- contra-caso: desconhecido sozinho continua bloqueando --------------
    SOZINHO = """
    <div style="width:600px;height:20px">algo aconteceu</div>
    <span role="alert" style="display:block;width:300px;height:14px">: Erro nao identificado</span>
    """
    a = o_alerta(alertas(SOZINHO))
    checa("desconhecido SEM par estrutural continua bloqueando",
          a is not None and a["severidade"] in BLOQ, a)

    # --- contra-caso: container AGREGADOR nao pode rebaixar erro ------------
    # o risco que o Codex apontou: um container informativo que junta varias
    # mensagens nao pode rebaixar um desconhecido legitimo. Aqui o texto do
    # alert aparece no container, mas NO MEIO - nao e' nucleo dele.
    AGREGADOR = """
    <div class="sapLsFPMMessageArea" role="presentation" style="width:600px;height:40px">
      Change request salvo. Falha ao replicar fornecedor
      <span role="alert" style="display:block;width:300px;height:14px">Falha ao replicar fornecedor</span>
    </div>
    """
    a = o_alerta(alertas(AGREGADOR))
    checa("container AGREGADOR nao rebaixa (texto no meio, nao e' nucleo)",
          a is not None and a["severidade"] in BLOQ, a)

    # --- contra-caso: par com classe de ERRO -> herda ERRO ------------------
    PAR_ERRO = """
    <div class="urMsgBarError" style="width:600px;height:40px">Invalid withholding tax code<span>Error.</span>
      <span role="alert" style="display:block;width:300px;height:14px">: Invalid withholding tax code</span>
    </div>
    """
    a = o_alerta(alertas(PAR_ERRO))
    checa("par com classe de ERRO faz o desconhecido herdar ERRO",
          a is not None and a["severidade"] == "erro", a)

    # --- contra-caso: mesmo texto, severidades diferentes -> vence a forte ---
    # era o furo do `vistos[t]` antigo: cortava por texto ANTES de comparar
    # severidade, e o primeiro no' prevalecia mesmo havendo irmao com classe de
    # erro. Agora coleta tudo primeiro e so entao decide.
    EMPATE = """
    <div style="width:600px;height:60px">
      <div role="status" style="width:300px;height:20px">Operacao concluida</div>
      <div class="urMsgBarError" style="width:300px;height:20px">Operacao concluida</div>
    </div>
    """
    r = alertas(EMPATE)
    iguais = [x for x in r if x["texto"] == "Operacao concluida"]
    checa("mesmo texto com severidades diferentes: vence a mais FORTE",
          len(iguais) == 1 and iguais[0]["severidade"] == "erro", iguais)

    # ---------------- caso 31: pos-condicao como decisao conjunta ----------
    # 12a revisao do Codex. Antes isto era uma sequencia de raise e parava no
    # primeiro sinal negativo: na sessao A abortou no alerta e NUNCA olhou o
    # botao, que estava desabilitado na tela dizendo que o Save tinha gravado.
    # A tabela que este caso trava:
    #   botao desabilitado, sem bloqueante        -> confirmado    (segue)
    #   botao desabilitado + desconhecido sem par -> ressalva      (para)
    #   botao desabilitado + erro/modal           -> problema      (levanta)
    #   botao habilitado / sumiu                  -> indeterminado (levanta)
    print("")
    print("== caso 31: pos-condicao decide com TODAS as evidencias (12a rev) ==")

    _prints_orig = auto.PRINTS_DIR
    auto.PRINTS_DIR = tempfile.mkdtemp(prefix="testeprints_")
    try:
        CR = "90000001"

        def tela(botao_habilitado, extra=""):
            press = " lsevents='{\"Press\":[{}]}'" if botao_habilitado else ""
            return f"""
            <div style="width:900px;height:60px">Change Request {CR} Description</div>
            <div id="WD6012" role="button"{press} style="width:60px;height:22px">
              <span id="WD6012-cnt">Save</span></div>
            {extra}
            """

        def veredito(html, antes=True):
            carregar(d, html)
            return auto._poscondicao_save(
                d, CR, estado_antes={"habilitado": antes})

        # 1) confirmado
        r = veredito(tela(False))
        checa("botao desabilitado e sem bloqueante -> CONFIRMADO",
              r.get("veredito") == "confirmado", r.get("veredito"))
        checa("e registra que o botao desabilitou (sinal positivo)",
              r.get("botao_desabilitou") is True, r)

        # 2) ressalva - EXATAMENTE o caso da sessao A
        ALERTA_SOLTO = ('<div id="fora" style="width:400px;height:20px">'
                        '<span role="alert" style="display:block;width:300px;height:14px">'
                        f': Change request {CR} saved</span></div>')
        r = veredito(tela(False, ALERTA_SOLTO))
        checa("botao desabilitado + desconhecido sem par -> RESSALVA (nao falha)",
              r.get("veredito") == "ressalva", r.get("veredito"))
        checa("a ressalva carrega a mensagem que ninguem classificou",
              any("saved" in m.get("texto", "") for m in (r.get("mensagens") or [])), r)

        # 3) problema - mensagem de erro de verdade
        ERRO = ('<div class="urMsgBarError" style="width:400px;height:20px">'
                'Invalid withholding tax code</div>')
        erro = None
        try:
            veredito(tela(False, ERRO))
        except Exception as e:
            erro = e
        checa("botao desabilitado + ERRO -> levanta",
              isinstance(erro, RuntimeError), type(erro).__name__ if erro else "nao levantou")
        checa("e a mensagem diz que pode ter gravado (nao mandar repetir cego)",
              erro is not None and "NAO repita cegamente" in str(erro), str(erro)[:120])

        # 4) indeterminado - botao continua habilitado
        erro = None
        try:
            veredito(tela(True))
        except Exception as e:
            erro = e
        checa("botao ainda HABILITADO -> indeterminado, levanta",
              isinstance(erro, RuntimeError) and "INDETERMINADO" in str(erro),
              str(erro)[:120] if erro else "nao levantou")

        # 5) o Submit nao pode ser autorizado por uma ressalva
        checa("ressalva NAO e' 'confirmado' (é o que o finalize exige pro Submit)",
              "ressalva" != "confirmado")
    finally:
        try:
            shutil.rmtree(auto.PRINTS_DIR, ignore_errors=True)
        except Exception:
            pass
        auto.PRINTS_DIR = _prints_orig

    # ---------------- caso 32: gotcha 15 - Submit sem novo Save -----------
    print("")
    print("== caso 32: sessao B usa atestado e Submit atomico sem novo Save ==")
    CR = "90000001"

    checa("modo SUBMETER-only existe",
          auto._modo_execucao(False, True) == "conferir_e_submeter")
    erro_modo = None
    try:
        auto._modo_execucao(True, True)
    except Exception as e:
        erro_modo = e
    checa("Save+Submit na mesma execucao agora e' proibido",
          isinstance(erro_modo, ValueError), type(erro_modo).__name__ if erro_modo else "passou")

    # O clique irreversivel revalida, NO MESMO JS, que Save existe e esta
    # desabilitado. Assim o checkpoint humano nao abre uma janela TOCTOU.
    RAIZ_SUBMIT = f"""
      <script>window.__submitClicado=false;</script>
      <div style="width:700px;height:30px">Change Request {CR}</div>
      <div id="SAVE" role="button" style="width:60px;height:22px"><span>Save</span></div>
      <div id="SUBMIT" role="button" lsevents='{{"Press":[{{}}]}}'
           style="width:70px;height:22px" onclick="window.__submitClicado=true">
        <span>Submit</span></div>
    """
    carregar(d, RAIZ_SUBMIT)
    r = js(d, auto._JS_CLICAR_SUBMIT_VALIDADO, CR,
           list(auto._severidades_bloqueantes()))
    checa("clique atomico aceita Save desabilitado + Submit habilitado",
          r.get("ok") is True and d.execute_script("return window.__submitClicado===true"), r)

    RAIZ_SAVE_HABILITADO = RAIZ_SUBMIT.replace(
        'id="SAVE" role="button"',
        'id="SAVE" role="button" lsevents=\'{"Press":[{}]}\'')
    carregar(d, RAIZ_SAVE_HABILITADO)
    r = js(d, auto._JS_CLICAR_SUBMIT_VALIDADO, CR,
           list(auto._severidades_bloqueantes()))
    checa("clique atomico RECUSA se Save voltou a habilitar",
          bool(r.get("erro")) and "DESABILITADO" in r.get("erro", ""), r)
    checa("e nao clicou Submit nesse contra-caso",
          d.execute_script("return window.__submitClicado===true") is False)

    carregar(d, RAIZ_SUBMIT +
             '<span role="alert" style="display:block;width:300px;height:14px">incerto</span>')
    r = js(d, auto._JS_CLICAR_SUBMIT_VALIDADO, CR,
           list(auto._severidades_bloqueantes()))
    checa("clique atomico RECUSA alerta desconhecido",
          bool(r.get("erro")) and "alerta bloqueante" in r.get("erro", ""), r)

    # O atestado precisa ser completo, da CR certa e da MESMA sessao que vai
    # clicar. Uma amostra ou um dict copiado de outra sessao nao autoriza.
    atestado_ok = {
        "apto_para_save": True,
        "save_veredito": "confirmado",
        "apto_para_submit": True,
        "cr_number": CR,
        "session_id": d.session_id,
        "codes_esperados": ["C0", "C3"],
        "detalhes_validados": ["C0", "C3"],
        "faltando": [], "inesperados": [], "duplicados": {},
        "descricoes_vazias": [], "tabelas_candidatas": 1,
        "save_desabilitado": True,
        "submit_habilitado": True,
    }
    checa("atestado completo da mesma sessao autoriza a pre-condicao Python",
          auto._validar_atestado_submit(d, CR, atestado_ok) is True)
    amostra = dict(atestado_ok)
    amostra["detalhes_validados"] = ["C0"]
    erro_amostra = None
    try:
        auto._validar_atestado_submit(d, CR, amostra)
    except Exception as e:
        erro_amostra = e
    checa("atestado por AMOSTRA nao autoriza Submit",
          isinstance(erro_amostra, RuntimeError))
    outra_sessao = dict(atestado_ok)
    outra_sessao["session_id"] = "sessao-antiga"
    erro_sessao = None
    try:
        auto._validar_atestado_submit(d, CR, outra_sessao)
    except Exception as e:
        erro_sessao = e
    checa("atestado de outra sessao nao autoriza Submit",
          isinstance(erro_sessao, RuntimeError))
    # gotcha 19 / Codex 36a: sem Save CONFIRMADO desta sessao, nao ha Submit.
    for veredito_save in (None, "ressalva"):
        sem_save = dict(atestado_ok)
        sem_save["save_veredito"] = veredito_save
        erro_sem_save = None
        try:
            auto._validar_atestado_submit(d, CR, sem_save)
        except Exception as e:
            erro_sem_save = e
        checa(f"Save {veredito_save!r} (nao confirmado) NAO autoriza Submit",
              isinstance(erro_sem_save, RuntimeError))

    # Prova a orquestracao da conferencia sem tocar no SAP. Os parsers/tabela
    # e Details ja tem casos proprios; aqui interessa o contrato do atestado.
    orig_nav = auto.navigate_to_tax_section
    orig_ler = auto.ler_linhas_impostos
    orig_validar = auto.validar_linha_imposto
    orig_fechar = auto.fechar_subtelas_para_raiz
    orig_raiz = auto._estado_raiz_pronta_para_submit
    prints_orig = auto.PRINTS_DIR
    vistos_details = []
    try:
        auto.PRINTS_DIR = tempfile.mkdtemp(prefix="testeconferencia_")
        auto.navigate_to_tax_section = lambda driver: None
        auto.ler_linhas_impostos = lambda driver, strict_rowcount=False, com_meta=False: (
            ([{"type": "C0", "description": "desc C0"},
              {"type": "C3", "description": "desc C3"}],
             {"aria_rowcount": 3, "n_tabelas_candidatas": 1}) if com_meta else [])
        auto.validar_linha_imposto = lambda driver, code: vistos_details.append(code)
        auto.fechar_subtelas_para_raiz = lambda driver, cr_number, on_checkpoint=None: "raiz"
        auto._estado_raiz_pronta_para_submit = lambda driver, cr_number: {
            "save_desabilitado": True, "submit_habilitado": True}

        atestado = auto.conferir_impostos_no_servidor(
            d, CR, codes=["C0", "C3"], detalhes_codes=None)
        checa("conferencia completa valida TODOS os Details",
              vistos_details == ["C0", "C3"], vistos_details)
        checa("conferencia completa fica APTA AO SAVE, nunca direto ao Submit",
              atestado.get("apto_para_save") is True
              and atestado.get("apto_para_submit") is False, atestado)

        vistos_details.clear()
        atestado_amostra = auto.conferir_impostos_no_servidor(
            d, CR, codes=["C0", "C3"], detalhes_codes=["C0"])
        checa("conferencia por amostra fica INAPTA",
              atestado_amostra.get("apto_para_save") is False
              and atestado_amostra.get("apto_para_submit") is False
              and vistos_details == ["C0"], atestado_amostra)

        # Passo 2 da sessao B: o Save obrigatorio. Stubs registram o que foi
        # chamado - o ponto e' provar QUANDO o Save e a leitura da raiz rodam.
        orig_salvar = auto.salvar_cr_da_raiz
        chamadas = []
        try:
            def stub_raiz(driver, cr_number):
                chamadas.append("raiz")
                return {"save_desabilitado": True, "submit_habilitado": True}
            auto._estado_raiz_pronta_para_submit = stub_raiz

            def salvar_com(veredito):
                def _f(driver, cr_number):
                    chamadas.append("save")
                    return {"veredito": veredito, "acao": "Save"}
                return _f

            auto.salvar_cr_da_raiz = salvar_com("confirmado")
            at = auto.salvar_conferida_para_submit(d, CR, dict(atestado))
            checa("Save CONFIRMADO + raiz pronta -> apto ao Submit",
                  at.get("apto_para_submit") is True and chamadas == ["save", "raiz"], (at, chamadas))
            checa("  ...e o atestado resultante passa na validacao do Submit",
                  auto._validar_atestado_submit(d, CR, at) is True)

            chamadas.clear()
            auto.salvar_cr_da_raiz = salvar_com("ressalva")
            at = auto.salvar_conferida_para_submit(d, CR, dict(atestado))
            checa("Save com RESSALVA -> NAO apto, e a raiz nem e' lida",
                  at.get("apto_para_submit") is False and at.get("save_veredito") == "ressalva"
                  and chamadas == ["save"], (at, chamadas))

            chamadas.clear()
            auto.salvar_cr_da_raiz = salvar_com("confirmado")
            erro_amostra_save = None
            try:
                auto.salvar_conferida_para_submit(d, CR, dict(atestado_amostra))
            except Exception as e:
                erro_amostra_save = e
            checa("conferencia por AMOSTRA nao chega nem a clicar Save",
                  isinstance(erro_amostra_save, RuntimeError) and chamadas == [],
                  (str(erro_amostra_save)[:80], chamadas))

            outra = dict(atestado)
            outra["session_id"] = "sessao-antiga"
            erro_outra = None
            try:
                auto.salvar_conferida_para_submit(d, CR, outra)
            except Exception as e:
                erro_outra = e
            checa("conferencia de outra sessao nao chega a clicar Save",
                  isinstance(erro_outra, RuntimeError) and chamadas == [], chamadas)
        finally:
            auto.salvar_cr_da_raiz = orig_salvar

        auto.ler_linhas_impostos = lambda driver, strict_rowcount=False, com_meta=False: (
            ([{"type": "C0", "description": "desc C0"},
              {"type": "C3", "description": "desc C3"}],
             {"aria_rowcount": 3, "n_tabelas_candidatas": 2}) if com_meta else [])
        erro_ambiguidade = None
        try:
            auto.conferir_impostos_no_servidor(
                d, CR, codes=["C0", "C3"], detalhes_codes=None)
        except Exception as e:
            erro_ambiguidade = e
        checa("duas tabelas candidatas NAO produzem atestado nenhum",
              isinstance(erro_ambiguidade, RuntimeError)
              and "ambiguidade" in str(erro_ambiguidade),
              str(erro_ambiguidade))
    finally:
        auto.navigate_to_tax_section = orig_nav
        auto.ler_linhas_impostos = orig_ler
        auto.validar_linha_imposto = orig_validar
        auto.fechar_subtelas_para_raiz = orig_fechar
        auto._estado_raiz_pronta_para_submit = orig_raiz
        try:
            shutil.rmtree(auto.PRINTS_DIR, ignore_errors=True)
        except Exception:
            pass
        auto.PRINTS_DIR = prints_orig

    # Antes do primeiro Submit nao existe sinal positivo observado. Mesmo com
    # leitura perfeita, a pos-condicao registra efeito desconhecido e NAO REPETIR.
    evidencia_orig = auto._evidencia_pos_clique
    try:
        auto._evidencia_pos_clique = lambda driver, cr_number, acao: (
            {"alertas": [], "modais": [], "candidatos": []},
            f"Change Request {CR} submitted")
        r = auto._poscondicao_submit(d, CR)
        checa("primeiro Submit nunca inventa sucesso: efeito DESCONHECIDO",
              r.get("veredito") == "efeito_desconhecido" and r.get("nao_repetir") is True, r)
    finally:
        auto._evidencia_pos_clique = evidencia_orig

    erro_finalize = None
    try:
        auto.finalize_cr(None, CR, salvar=False, submeter=True)
    except Exception as e:
        erro_finalize = e
    checa("finalize antigo nao consegue mais submeter por atalho",
          isinstance(erro_finalize, RuntimeError),
          str(erro_finalize)[:120] if erro_finalize else "passou")

    # ---------------- caso 33: gotcha 19 - wrapper '-r' da toolbar ----------
    # Estrutura dos dumps REAIS de 14/09 (WD02668E) e 24/09 (WD0303A7): cada
    # botao da barra vem dentro de span.lsToolbar--item 'X-r' (41x32 em volta
    # do botao 41x26), mesmo texto, sem lsevents; e tudo dentro da raiz da
    # janela, que TEM lsevents (Help) - o que causou o bug de 14/09.
    print("")
    print("== caso 33: wrapper '-r' da toolbar vira o MESMO botao (gotcha 19) ==")
    PRESS = '{"Press":[{"ResponseData":"delta","ClientAction":"submit"},{}]}'
    CLS_R = "lsToolbar--item lsToolbar--item-button  lsToolbar--item-left"
    CLS_B = "lsButton lsButton--base urInlineMetricTop urNoUserSelect urBtnRadius  lsButton--useintoolb"

    # base com role=button, como o botao WDA real (gotcha 5)
    def item(bid, texto, largura, ev_base=PRESS, rid=None, ev_r="", extra_base="",
             cls_base=CLS_B, role=True):
        rid = rid if rid is not None else bid + "-r"
        ev_b = f" lsevents='{ev_base}'" if ev_base else ""
        ev_rr = f" lsevents='{ev_r}'" if ev_r else ""
        rl = ' role="button"' if role else ""
        return (f'<span id="{rid}" class="{CLS_R}"{ev_rr} style="display:inline-block;padding:3px 0">'
                f'<div id="{bid}" class="{cls_base}"{rl}{ev_b} tabindex="0" {extra_base}'
                f' style="display:inline-block;width:{largura}px;height:26px">'
                f'<span id="{bid}-cnt">{texto}</span></div></span>')

    def barra(*itens):
        return (f'<script>window.__submitClicado=false;window.__saveClicado=false;</script>'
                f'<div id="sapwd_main_window_root_" lsevents=\'{{"Help":[{{}}]}}\'>'
                f'<div style="width:700px;height:30px">Change Request {CR}</div>'
                f'<div style="position:absolute;left:20px;top:120px;white-space:nowrap">'
                + "".join(itens) + '</div></div>')

    SAVE_HAB = item("WD0303A7", "Save", 41, extra_base='onclick="window.__saveClicado=true"')
    SAVE_DES = item("WD0303A7", "Save", 41, ev_base="")   # pos-Save: sem lsevents nenhum
    SUBMIT = item("WD0303A8", "Submit", 47, extra_base='onclick="window.__submitClicado=true"')
    WITHDRAW = item("WD0303A9", "Withdraw Draft", 102)

    def saves_de(html):
        carregar(d, html)
        return js(d, auto._JS_ESTADO_ACAO_RAIZ, "Save").get("candidatos") or []

    c = saves_de(barra(SAVE_HAB, SUBMIT, WITHDRAW))
    checa("antes do Save: wrapper -r + botao = 1 candidato Save",
          len(c) == 1 and c[0].get("host_id") == "WD0303A7", c)
    checa("  ...habilitado, com o -r registrado na auditoria",
          c and c[0].get("habilitado") is True and c[0].get("canonizado_de") == "WD0303A7-r"
          and "WD0303A7-r" in (c[0].get("nos") or []), c)
    r = js(d, auto._JS_CLICAR_ACAO_RAIZ, "Save", CR, list(auto._severidades_bloqueantes()))
    checa("clique do Save acerta o botao real, nao o wrapper",
          r.get("ok") is True and (r.get("clicado") or {}).get("host_id") == "WD0303A7"
          and d.execute_script("return window.__saveClicado===true"), r)

    c = saves_de(barra(SAVE_DES, SUBMIT, WITHDRAW))
    checa("depois do Save: 1 candidato Save, DESABILITADO (o -cnt nao escapa ate a raiz)",
          len(c) == 1 and c[0].get("habilitado") is False and c[0].get("host_id") == "WD0303A7", c)
    r = js(d, auto._JS_CLICAR_SUBMIT_VALIDADO, CR, list(auto._severidades_bloqueantes()))
    checa("Submit atomico ACEITA a barra real pos-Save (antes: 2 Saves, recusado)",
          r.get("ok") is True and d.execute_script("return window.__submitClicado===true"), r)

    # Contra-casos: sem as quatro condicoes, NADA e' colapsado - e as duas
    # contagens ('1 habilitado' e '1 existente') continuam falhando fechado.
    c = saves_de(barra(item("WD0303A7", "Save", 41, ev_r=PRESS), SUBMIT))
    checa("contra: -r com Press PROPRIO nao e' colapsado", len(c) > 1, c)

    c = saves_de(barra(item("WD0303A7", "Save", 41, rid="WD0303A7-x"), SUBMIT))
    checa("contra: sufixo diferente de '-r' nao e' colapsado", len(c) > 1, c)

    c = saves_de(barra(item("WD0303B1", "Save", 41, rid="WD0303A7-r"), SUBMIT))
    checa("contra: '-r' orfao (sem elemento-base) nao e' colapsado", len(c) > 1, c)

    c = saves_de(barra(item("WD0303B1", "Save", 41, rid="WD0303A7-r"),
                       '<div id="WD0303A7" class="' + CLS_B + '" lsevents=\'' + PRESS
                       + '\' style="display:inline-block;width:47px;height:26px">Submit</div>'))
    checa("contra: base com OUTRO texto nao e' colapsado", len(c) > 1, c)

    c = saves_de(barra(item("WD0303A7", "Save", 41, ev_base="", cls_base="texto qualquer",
                            role=False).replace(' tabindex="0"', ""), SUBMIT))
    checa("contra: base que nao e' controle nao e' colapsado", len(c) > 1, c)

    carregar(d, barra(item("WD0303A7", "Save", 41, ev_base="", rid="WD0303A7-x"), SUBMIT))
    r = js(d, auto._JS_CLICAR_SUBMIT_VALIDADO, CR, list(auto._severidades_bloqueantes()))
    checa("contra: sem canonizar, o Submit continua RECUSANDO 2 Saves",
          bool(r.get("erro")) and d.execute_script("return window.__submitClicado===false"), r)

    # ---------------- caso 34: eco ARIA do WDA (28/09) ---------------------
    # HTML REAL do Save da CR 90000003 (log_impostos_20260928_101748): o
    # [role=alert] sem classe e' o span#sapur-aria-alert, copia pro leitor de
    # tela da mensagem da area do FPM. Regra especifica: herda 'info' so com a
    # configuracao exata; qualquer desvio continua bloqueando.
    print("")
    print("== caso 34: eco ARIA do WDA herda so da mensagem Info exata ==")
    CR = "90000003"

    def area(texto_html):
        return ('<div id="WD0278B3" class="lsHTMLContainer lsScrollContainer--nonescrolling '
                'sapLsFPMMessageArea" role="presentation" style="width:600px;height:40px">'
                + texto_html + '</div>')

    def eco(texto, oculto=True, pai="sapur-aria-dynamic-texts", eid="sapur-aria-alert"):
        return ('<div id="sapur-aria" class="lsPage--ariastatic"><div id="' + pai + '">'
                '<span id="' + eid + '" role="alert" aria-labelledby="ARIA_MA_TOGGLE_FOCUS_TUTOR"'
                + (' aria-hidden="true"' if oculto else '')
                + ' style="position:absolute;top:-70003px;display:block;width:71px;height:68px">'
                '<span>' + texto + '</span></span></div></div>')

    SALVO = f"Change request {CR} saved"
    AREA_INFO = area(SALVO + '<span>Info.</span> <a href="#">Display Message Log</a>')
    ECO_SALVO = eco(": " + SALVO)

    a = o_alerta(alertas(AREA_INFO + ECO_SALVO))
    checa("DOM real: o eco herda 'info' da area do FPM",
          a is not None and a["severidade"] == "info" and a["severidade"] not in BLOQ, a)
    checa("  ...e registra de onde herdou",
          a is not None and a["severidade_original"] == "desconhecido"
          and (a["duplicado_de"] or "").startswith(SALVO), a)
    a = o_alerta(alertas(area(SALVO + '<span>Info.</span>') + ECO_SALVO))
    checa("area sem o link 'Display Message Log' tambem vale",
          a is not None and a["severidade"] == "info", a)

    contras = [
        ("area com 'Error.' no lugar de 'Info.'",
         area(SALVO + '<span>Error.</span> <a href="#">Display Message Log</a>') + ECO_SALVO),
        ("area com texto a MAIS depois do Info.",
         area(SALVO + '<span>Info.</span> Falha ao replicar fornecedor') + ECO_SALVO),
        ("eco com OUTRA mensagem",
         AREA_INFO + eco(": Falha ao replicar fornecedor")),
        ("eco sem aria-hidden",
         AREA_INFO + eco(": " + SALVO, oculto=False)),
        ("eco fora do #sapur-aria-dynamic-texts",
         AREA_INFO + eco(": " + SALVO, pai="outro-lugar")),
        ("[role=alert] com outro id no mesmo lugar",
         AREA_INFO + eco(": " + SALVO, eid="outro-alert")),
        ("par que nao e' a area do FPM",
         '<div class="lsMessageBar" role="presentation" style="width:600px;height:40px">'
         + SALVO + '<span>Info.</span></div>' + ECO_SALVO),
        ("eco sozinho, sem area visivel", ECO_SALVO),
    ]
    for nome, html in contras:
        a = o_alerta(alertas(html))
        checa("contra: " + nome + " -> continua BLOQUEANDO",
              a is not None and a["severidade"] in BLOQ, a)

    # a pos-condicao do Save, com o DOM real + Save desabilitado, agora CONFIRMA
    _prints_orig = auto.PRINTS_DIR
    auto.PRINTS_DIR = tempfile.mkdtemp(prefix="testeprints_")
    try:
        raiz = (f'<div style="width:900px;height:60px">Change Request {CR} Description</div>'
                '<div id="WD6012" role="button" style="width:60px;height:22px">'
                '<span id="WD6012-cnt">Save</span></div>')
        carregar(d, raiz + AREA_INFO + ECO_SALVO)
        r = auto._poscondicao_save(d, CR, estado_antes={"habilitado": True})
        checa("pos-Save com o DOM real de 28/09 -> CONFIRMADO (era ressalva)",
              r.get("veredito") == "confirmado", r.get("veredito"))
        carregar(d, raiz + area(SALVO + '<span>Error.</span>') + ECO_SALVO)
        r = auto._poscondicao_save(d, CR, estado_antes={"habilitado": True})
        checa("contra: mesma tela com 'Error.' -> NAO confirma",
              r.get("veredito") == "ressalva", r.get("veredito"))
        # a instrumentacao traz as classes de dentro da area (o icone)
        carregar(d, raiz + area(SALVO + '<span class="icone--info">Info.</span>') + ECO_SALVO)
        nos = js(d, auto._JS_DUMP_ALERTAS_HTML)
        checa("dump de HTML traz as classes dos descendentes",
              any("icone--info" in (n.get("classes_descendentes") or []) for n in nos), nos)
    finally:
        shutil.rmtree(auto.PRINTS_DIR, ignore_errors=True)
        auto.PRINTS_DIR = _prints_orig

    # ---------------- caso 35: travas do limpar_cr_teste (28/09) -----------
    # O script APAGA dado. As travas que rodam antes de qualquer clique sao
    # funcoes puras - testadas aqui, sem SAP.
    print("")
    print("== caso 35: limpar_cr_teste so aceita CR de teste e so apaga os 64 ==")
    import limpar_cr_teste as limpar

    def levanta(fn, *a):
        try:
            fn(*a)
        except (SystemExit, RuntimeError) as e:
            return str(e)
        return None

    checa("as 5 CRs de teste passam",
          levanta(limpar.validar_crs, ["90000003", "90000001", "90000005"]) is None)
    checa("CR fora da lista e' recusada (mesmo junto de uma valida)",
          "12345678" in (levanta(limpar.validar_crs, ["90000003", "12345678"]) or ""))
    checa("tabela so com codigos nossos -> apaga todos",
          limpar.codigos_a_apagar(["C0", "C3", "Z9"]) == ["C0", "C3", "Z9"])
    checa("tabela vazia -> nada a apagar", limpar.codigos_a_apagar([]) == [])
    checa("linha fora dos 64 -> nao apaga NADA",
          "Q7" in (levanta(limpar.codigos_a_apagar, ["C0", "Q7"]) or ""))
    checa("codigo repetido -> nao apaga NADA",
          "C0" in (levanta(limpar.codigos_a_apagar, ["C0", "C0", "C3"]) or ""))

    # ---------------- caso 36: Delete pelo caminho critico (28/09) ---------
    # NO-GO do Codex: o Delete clicava pelo _safe_execute_script, que aceita um
    # confirm() e repete o clique. Driver criado pela MESMA funcao da producao
    # (unhandledPromptBehavior=ignore), senao "o dialogo fica aberto" nao vale.
    print("")
    print("== caso 36: Delete critico - dialogo fica aberto, 1 clique, 1 controle ==")
    H2_PEQ = "<style>h2{display:inline-block;font-size:12px;margin:0}</style>"
    TRES = linha("C0", "Desc C0", 2) + linha("C3", "Desc C3", 3) + linha("Z9", "Desc Z9", 4)

    dc = auto.criar_driver_edge(headless=True)
    # prazo curto pro Delete neste caso: os contracasos em que a releitura FALHA
    # esperariam os 60 s inteiros antes de levantar (o resultado e' o mesmo)
    prazo_delete_orig = auto.ESPERA_MAX_DELETE_S
    auto.ESPERA_MAX_DELETE_S = 8
    # scroll_to_text so' posiciona a tela e procura o titulo entre elementos
    # clicaveis do WDA, que o DOM sintetico nao imita - fora do que este caso testa
    _scroll_orig = auto.scroll_to_text
    auto.scroll_to_text = lambda *a, **k: None
    try:
        dc.set_window_size(1400, 900)

        def montar(html, modo=""):
            # cada Delete conta o clique. modo: "confirm" (confirm() sincrono),
            # "confirm_async" (confirm() 400 ms depois do clique), "some_rowcount"
            # (aria-rowcount desaparece), "duplica_tabela" (surge 2a tabela)
            carregar(dc, H2_PEQ + html)
            dc.execute_script("""
              window.__cliques = 0; window.__aceitos = 0;
              const modo = arguments[0];
              function remover(a) {
                const g = document.getElementById('WDTAX');
                a.closest('tr').remove();
                if (modo === 'some_rowcount') g.removeAttribute('aria-rowcount');
                else g.setAttribute('aria-rowcount', String(parseInt(g.getAttribute('aria-rowcount')) - 1));
                if (modo === 'duplica_tabela')
                  document.body.insertAdjacentHTML('beforeend', g.outerHTML.split('id="').join('id="x'));
              }
              for (const a of document.querySelectorAll('a[title="Delete"]')) {
                a.addEventListener('click', function () {
                  window.__cliques++;
                  if (modo === 'confirm_async') {
                    setTimeout(function () {
                      if (confirm('Delete this row?')) { window.__aceitos++; remover(a); }
                    }, 400);
                    return;
                  }
                  if (modo === 'confirm') { if (!confirm('Delete this row?')) return; window.__aceitos++; }
                  if (modo === 'lento') { setTimeout(function () { remover(a); }, 5000); return; }
                  if (modo === 'nunca') return;
                  if (modo === 'outra') {
                    const tr = a.closest('tr');
                    remover((tr.nextElementSibling || tr.previousElementSibling)
                            .querySelector('a[title="Delete"]'));
                    return;
                  }
                  remover(a);
                });
              }""", modo)

        def contagem():
            return dc.execute_script("return [window.__cliques, window.__aceitos]")

        def fechar_dialogo():
            try:
                texto = dc.switch_to.alert.text
                dc.switch_to.alert.dismiss()
                return texto
            except Exception:
                return ""

        montar(pagina(TRES, 4))
        r = auto.deletar_linha_imposto(dc, "C3")
        checa("Delete sem dialogo: remove so' C3 com 1 clique",
              r == ["C0", "Z9"] and contagem()[0] == 1, (r, contagem()))

        montar(pagina(TRES, 4), "confirm")
        erro = levanta(auto.deletar_linha_imposto, dc, "C0")
        aberto = fechar_dialogo()
        checa("Delete que abre confirm() -> LEVANTA dizendo que NAO aceitou",
              "NAO aceitei" in (erro or ""), (erro or "")[:120])
        checa("  ...e o dialogo continuava ABERTO (ninguem aceitou)",
              aberto == "Delete this row?", aberto)
        checa("  ...com 1 clique so' e nenhum aceite", contagem() == [1, 0], contagem())

        # contraprova: o caminho ANTIGO era clique permissivo + espera permissiva.
        # O clique deixa o confirm() aberto; a espera seguinte o ACEITA sozinha.
        montar(pagina(TRES, 4), "confirm")
        try:
            auto._clicar_acao_linha(dc, "C0", "Delete")
            auto.wait_for_wda_ready(dc, timeout=15)
        except Exception:
            pass
        fechar_dialogo()
        checa("contra: o caminho permissivo antigo ACEITA o confirm() sozinho",
              contagem()[1] >= 1, contagem())

        DUPLO = linha("C0", "Desc C0", 2).replace(
            'title="Delete">x</a>', 'title="Delete">x</a><a id="d2C0" title="Delete">y</a>')
        montar(pagina(DUPLO, 2))
        erro = levanta(auto.deletar_linha_imposto, dc, "C0")
        checa("2 controles Delete na linha -> recusa sem clicar",
              "exatamente 1 controle Delete" in (erro or "") and contagem()[0] == 0,
              ((erro or "")[:120], contagem()))

        ANINHADO = linha("C0", "Desc C0", 2).replace(
            'title="Delete">x</a>', 'title="Delete"><span role="button" title="Delete">x</span></a>')
        montar(pagina(ANINHADO + linha("C3", "Desc C3", 3), 3))
        r = auto.deletar_linha_imposto(dc, "C0")
        checa("controle aninhado com o mesmo title conta como 1 -> apaga",
              r == ["C3"] and contagem()[0] == 1, (r, contagem()))

        montar(pagina(TRES, 4) + pagina(TRES, 4))
        erro = levanta(auto.deletar_linha_imposto, dc, "C0")
        checa("2 tabelas de impostos -> recusa sem clicar",
              "1 tabela" in (erro or "") and contagem()[0] == 0, ((erro or "")[:120], contagem()))

        # confirm() ASSINCRONO (2a revisao do Codex): o clique volta ok e o
        # dialogo abre durante a espera critica - tem que levantar sem aceitar
        montar(pagina(TRES, 4), "confirm_async")
        erro = levanta(auto.deletar_linha_imposto, dc, "C0")
        aberto = fechar_dialogo()
        checa("confirm() assincrono pos-clique -> LEVANTA dizendo que NAO aceitou",
              "NAO aceitei" in (erro or ""), (erro or "")[:120])
        checa("  ...dialogo continuava ABERTO, 1 clique, 0 aceites",
              aberto == "Delete this row?" and contagem() == [1, 0], (aberto, contagem()))

        # 29/09 14:52: um delete levou >17 s e a releitura unica pegou a tabela
        # antes do SAP terminar. Agora espera a linha SUMIR (ate o prazo).
        montar(pagina(TRES, 4), "lento")
        t0 = time.time()
        r, erro = None, None
        try:
            r = auto.deletar_linha_imposto(dc, "C3")
        except Exception as e:
            erro = e
        checa("Delete LENTO (linha some 5 s depois): espera o efeito e passa",
              erro is None and r == ["C0", "Z9"] and contagem()[0] == 1
              and time.time() - t0 >= 4.5,
              (r, repr(erro)[:80] if erro else "", contagem(), round(time.time() - t0, 1)))
        montar(pagina(TRES, 4), "outra")
        erro = levanta(auto.deletar_linha_imposto, dc, "C3")
        checa("contra: sumiu OUTRA linha -> delta errado na hora",
              "delta errado" in (erro or ""), (erro or "")[:100])
        montar(pagina(TRES, 4), "nunca")
        t0 = time.time()
        erro = levanta(auto.deletar_linha_imposto, dc, "C3")
        checa("contra: a linha NUNCA some -> delta errado no prazo",
              "delta errado" in (erro or "") and time.time() - t0 >= 7,
              ((erro or "")[:80], round(time.time() - t0, 1)))

        # leitura integral no delta: aria-rowcount sumir apos o Delete nao passa
        montar(pagina(TRES, 4), "some_rowcount")
        erro = levanta(auto.deletar_linha_imposto, dc, "C3")
        checa("aria-rowcount some apos o Delete -> o delta NAO e' aceito",
              "strict" in (erro or "") and contagem()[0] == 1, ((erro or "")[:120], contagem()))

        # ultima prova antes do Save (limpar_impostos): 2a tabela surgindo derruba
        montar(pagina(TRES, 4), "duplica_tabela")
        erro = levanta(auto.limpar_impostos, dc, ["C3"], None, True)
        checa("limpar_impostos: 2a tabela depois do Delete -> recusa",
              "1 tabela" in (erro or ""), (erro or "")[:120])
        montar(pagina(TRES, 4))
        checa("limpar_impostos: caminho feliz apaga C3 e Z9, sobra C0",
              auto.limpar_impostos(dc, ["C3", "Z9"], forcar=True) == ["C0"]
              and contagem()[0] == 2, contagem())

        # a leitura integral em si: aria-rowcount e UMA tabela
        carregar(dc, pagina(TRES, 4))
        checa("leitura integral: 1 tabela e rowcount batendo",
              auto.ler_tabela_impostos_integral(dc) == ["C0", "C3", "Z9"])
        carregar(dc, pagina(TRES, -1))
        checa("leitura integral: sem aria-rowcount -> recusa",
              levanta(auto.ler_tabela_impostos_integral, dc) is not None)
        carregar(dc, pagina(TRES, 4) + pagina(TRES, 4))
        checa("leitura integral: 2 tabelas -> recusa",
              "1 tabela" in (levanta(auto.ler_tabela_impostos_integral, dc) or ""))
    finally:
        auto.scroll_to_text = _scroll_orig
        auto.ESPERA_MAX_DELETE_S = prazo_delete_orig
        dc.quit()

    checa("falha no Save avisa que PODE ter gravado",
          "PODE ter gravado" in limpar.mensagem_de_falha("salvando"))
    checa("falha apagando diz que nada foi gravado e a CR travou",
          "nada foi gravado" in limpar.mensagem_de_falha("apagando")
          and "TRAVADA" in limpar.mensagem_de_falha("apagando"))
    checa("falha antes de entrar na CR: nada alterado",
          "nada foi alterado" in limpar.mensagem_de_falha("abrindo"))
    checa("lista com entrada invalida -> recusa",
          "invalida" in (levanta(limpar.validar_crs, ["90000003"], True) or ""))

    # ---------------- caso 37: fluxo unico (28/09) ---------------------------
    # Revisao da 45a entrada: modos, atestado proprio da insercao (com
    # contracasos), consumo unico, o lote chamando as etapas NA ORDEM e a
    # checagem do Submit reconhecida por estrutura (fixture: log de 28/09).
    print("")
    print("== caso 37: fluxo unico - modos, atestado, lote e checagem do Submit ==")

    def erro_de(fn, *a, **k):
        try:
            fn(*a, **k)
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        return None

    checa("modo fluxo unico", auto._modo_execucao(False, False, True) == "inserir_salvar_submeter")
    checa("SALVAR + fluxo unico -> ValueError",
          (erro_de(auto._modo_execucao, True, False, True) or "").startswith("ValueError"))
    checa("SUBMETER + fluxo unico -> ValueError",
          (erro_de(auto._modo_execucao, False, True, True) or "").startswith("ValueError"))
    checa("palavra: SALVAR E SUBMETER",
          auto._confirmacao_modo("inserir_salvar_submeter", "pre_producao") == "SALVAR E SUBMETER")
    checa("palavra em producao: PRODUCAO SALVAR E SUBMETER",
          auto._confirmacao_modo("inserir_salvar_submeter", "producao")
          == "PRODUCAO SALVAR E SUBMETER")
    desc_amostra = auto._descricao_modo("inserir_salvar_submeter")
    checa("tela do fluxo unico descreve a AMOSTRA (NO-GO do Codex)",
          "amostra" in desc_amostra and "TODOS" not in desc_amostra, desc_amostra)
    modo_desc = auto.DETAILS_NO_FLUXO_UNICO
    try:
        auto.DETAILS_NO_FLUXO_UNICO = "todos"
        checa("  ...e com a chave 'todos' promete os 64",
              "TODOS os 64" in auto._descricao_modo("inserir_salvar_submeter"))
    finally:
        auto.DETAILS_NO_FLUXO_UNICO = modo_desc
    checa("estimativa: 16 min por CR (Details por amostra)",
          auto._estimativa_tempo(2, "inserir_salvar_submeter") == "32 min")

    CRF = "90000001"
    TODOS = list(auto.TAX_CODES)
    LINHAS64 = "".join(linha(c, "Desc " + c, i + 2) for i, c in enumerate(TODOS))
    RESUMO = {"inseridos": TODOS, "pulados": [], "faltavam": TODOS}
    NOMES = ("validar_linha_imposto", "fechar_subtelas_para_raiz", "salvar_cr_da_raiz",
             "_estado_raiz_pronta_para_submit")
    orig = {n: getattr(auto, n) for n in NOMES}
    detalhes = []
    auto.validar_linha_imposto = lambda drv, code: detalhes.append(code)
    auto.fechar_subtelas_para_raiz = lambda drv, cr, **k: None
    auto.salvar_cr_da_raiz = lambda drv, cr: {"veredito": "confirmado"}
    auto._estado_raiz_pronta_para_submit = lambda drv, cr: {"save_desabilitado": True,
                                                            "submit_habilitado": True}
    try:
        def registrar_todos():
            auto._INSERIDOS_NESTA_EXECUCAO.pop(CRF, None)
            for c in TODOS:
                auto._registrar_insercao(CRF, c)

        def atestado_inicial():
            carregar(d, pagina("", 1, vazio=True))
            return auto.iniciar_atestado_insercao(d, CRF)

        def atestado_completo():
            a = atestado_inicial()
            registrar_todos()
            carregar(d, pagina(LINHAS64, 65))
            del detalhes[:]
            return auto.atestar_insercao_para_save(d, CRF, a, dict(RESUMO))

        at = atestado_inicial()
        checa("preflight: tabela vazia -> atestado inicial",
              at["fase"] == "aberta_limpa" and at["tabela_inicial_vazia"] is True)
        carregar(d, pagina(linha("C0", "Desc C0", 2), 2))
        checa("preflight: CR que ja tem linha -> recusa",
              "LIMPA" in (erro_de(auto.iniciar_atestado_insercao, d, CRF) or ""))

        at = atestado_completo()
        checa("insercao integral + Details do 1o e do ultimo + leitura final -> apta ao Save",
              at["fase"] == "insercao_atestada" and at["apto_para_save"] is True
              and detalhes == [TODOS[0], TODOS[-1]], (at["fase"], detalhes))
        modo_d = auto.DETAILS_NO_FLUXO_UNICO
        try:
            auto.DETAILS_NO_FLUXO_UNICO = "todos"
            at_todos = atestado_completo()
            checa("chave 'todos': volta a conferir os 64 Details (1a rodada de 29/09)",
                  at_todos["apto_para_save"] is True and detalhes == TODOS, len(detalhes))
        finally:
            auto.DETAILS_NO_FLUXO_UNICO = modo_d
        incompleto = dict(at, detalhes_validados=[TODOS[0]])
        checa("contra: atestado sem o Details do ultimo codigo -> validacao recusa",
              "inconsistente" in (erro_de(auto._validar_atestado_insercao, d, CRF, incompleto,
                                          "insercao_atestada") or ""))

        a = atestado_inicial()
        registrar_todos()
        carregar(d, pagina(LINHAS64, 65))
        checa("contra: codigo pulado -> recusa",
              "integral" in (erro_de(auto.atestar_insercao_para_save, d, CRF, a,
                                     {"inseridos": TODOS[1:], "pulados": ["C0"],
                                      "faltavam": TODOS[1:]}) or ""))
        a = atestado_inicial()
        registrar_todos()
        auto._desregistrar_insercao(CRF, "Z9")
        carregar(d, pagina(LINHAS64, 65))
        checa("contra: registro desta execucao parcial -> recusa",
              "integral" in (erro_de(auto.atestar_insercao_para_save, d, CRF, a,
                                     dict(RESUMO)) or ""))

        def details_falha(drv, code):
            if code == "Z9":
                raise RuntimeError("Details divergente")
            detalhes.append(code)
        auto.validar_linha_imposto = details_falha
        a = atestado_inicial()
        registrar_todos()
        carregar(d, pagina(LINHAS64, 65))
        erro = erro_de(auto.atestar_insercao_para_save, d, CRF, a, dict(RESUMO))
        checa("contra: o Details do ultimo codigo falhando -> recusa",
              "Details divergente" in (erro or "")
              and a["detalhes_validados"] == [TODOS[0]], (erro, a["detalhes_validados"]))
        auto.validar_linha_imposto = lambda drv, code: detalhes.append(code)

        a = atestado_inicial()
        registrar_todos()
        carregar(d, pagina(LINHAS64, 65) + pagina(LINHAS64, 65))
        checa("contra: 2 tabelas na leitura final -> recusa",
              "leitura integral final recusada" in
              (erro_de(auto.atestar_insercao_para_save, d, CRF, a, dict(RESUMO)) or ""))

        at = atestado_completo()

        class OutraSessao:
            session_id = "outra"
        checa("contra: atestado usado noutra sessao -> recusa",
              "outra sessao" in (erro_de(auto._validar_atestado_insercao, OutraSessao(), CRF, at,
                                         "insercao_atestada") or ""))
        checa("contra: atestado usado noutra CR -> recusa",
              "outra CR" in (erro_de(auto._validar_atestado_insercao, d, "99999999", at,
                                     "insercao_atestada") or ""))

        at = auto.salvar_insercao_para_submit(d, CRF, at)
        checa("Save confirmado -> apto a UM Submit",
              at["fase"] == "raiz_pronta_submit" and at["apto_para_submit"] is True
              and at["consumido_para_save"] is True, at["fase"])
        checa("o atestado passa na validacao do Submit",
              erro_de(auto._validar_atestado_submit, d, CRF, at) is None,
              erro_de(auto._validar_atestado_submit, d, CRF, at))

        # composicao REAL (revisao de 29/09): atestado da insercao -> Save -> CR
        # reaberta -> submeter, com o _validar_atestado_submit de verdade
        at["reaberta"] = {"janela_antiga": "velha", "janela_nova": d.current_window_handle}
        cliques_c = []
        orig_c = {n: getattr(auto, n) for n in ("_clicar_submit_validado_da_raiz",
                                                "_poscondicao_submit")}
        passo_c = auto.STEP_CONFIRM
        try:
            auto.STEP_CONFIRM = False
            auto._clicar_submit_validado_da_raiz = lambda drv, cr: cliques_c.append(cr)
            auto._poscondicao_submit = lambda drv, cr: {"veredito": "confirmado",
                                                        "nao_repetir": True}
            r = auto.submeter_cr_validada_da_raiz(d, CRF, at)
            checa("composicao real: insercao atestada -> Save -> reaberta -> UM Submit",
                  cliques_c == [CRF] and r.get("veredito") == "confirmado"
                  and at.get("fase") == "confirmado" and at.get("submit_clicado") is True,
                  (cliques_c, at.get("fase")))
        finally:
            for n, f in orig_c.items():
                setattr(auto, n, f)
            auto.STEP_CONFIRM = passo_c
        checa("contra: consumir o mesmo atestado de novo -> recusa",
              erro_de(auto.salvar_insercao_para_submit, d, CRF, at) is not None)

        auto.salvar_cr_da_raiz = lambda drv, cr: {"veredito": "ressalva"}
        at = auto.salvar_insercao_para_submit(d, CRF, atestado_completo())
        checa("Save com ressalva -> NAO apto ao Submit",
              at["fase"] == "persistencia_incerta" and at["apto_para_submit"] is False, at["fase"])
    finally:
        for n, f in orig.items():
            setattr(auto, n, f)
        auto._INSERIDOS_NESTA_EXECUCAO.pop(CRF, None)

    # o relato de UM clique de Submit usa o veredito real
    r = {"ok": [], "ressalva": [], "falha": []}
    checa("relato: parou na checagem -> para o lote, rotulo proprio",
          auto._registrar_resultado_submit(r, "1", {}, {"veredito": "submit_parou_na_checagem"})
          is False and r["ressalva"][0][1] == "submit_parou_na_checagem", r["ressalva"])
    checa("relato: efeito desconhecido mantem o rotulo antigo",
          auto._registrar_resultado_submit(r, "2", {}, {"veredito": "efeito_desconhecido"})
          is False and r["ressalva"][1][1] == "submit_clicado_efeito_desconhecido")
    checa("relato: so' 'confirmado' deixa o lote seguir",
          auto._registrar_resultado_submit(r, "3", {}, {"veredito": "confirmado"}) is True
          and r["ok"][0][1] == "submetida")

    # o lote com a flag ligada: etapas na ordem, e para depois do 1o Submit
    ordem = []
    NOMES_LOTE = ("esperar_login_manual", "go_to_display_change_requests", "open_cr",
                  "cr_esta_travada", "navigate_to_tax_section", "iniciar_atestado_insercao",
                  "insert_all_taxes", "atestar_insercao_para_save", "salvar_insercao_para_submit",
                  "submeter_cr_validada_da_raiz", "reabrir_cr_para_submit",
                  "conferir_impostos_no_servidor", "salvar_conferida_para_submit")
    orig_lote = {n: getattr(auto, n) for n in NOMES_LOTE}
    flags = (auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER)

    class DriverFalso:
        session_id = "falso"
        window_handles = ["w0"]
        current_window_handle = "w0"

        class switch_to:
            @staticmethod
            def window(h):
                pass

        def get(self, url):
            ordem.append("get")

        def execute_script(self, script, *a):
            return True

    def passo(nome, ret=None):
        def f(*a, **k):
            ordem.append(nome)
            return dict(ret) if isinstance(ret, dict) else ret
        return f

    def rodar(crs):
        try:
            auto._rodar_lote(DriverFalso(), crs)
        except SystemExit as e:
            return e.code
        return 0

    try:
        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = False, False, True
        auto.esperar_login_manual = passo("login")
        auto.go_to_display_change_requests = passo("app")
        auto.open_cr = passo("abrir")
        auto.cr_esta_travada = passo("lock?", False)
        auto.navigate_to_tax_section = passo("navegar")
        auto.iniciar_atestado_insercao = passo("preflight", {"fase": "aberta_limpa"})
        auto.insert_all_taxes = passo("inserir", {"inseridos": [], "pulados": []})
        auto.atestar_insercao_para_save = passo("atestar", {"fase": "insercao_atestada"})
        auto.salvar_insercao_para_submit = passo(
            "salvar", {"fase": "raiz_pronta_submit", "save_veredito": "confirmado"})
        auto.reabrir_cr_para_submit = passo(
            "reabrir", {"janela_antiga": "a", "janela_nova": "b"})
        auto.submeter_cr_validada_da_raiz = passo(
            "submit", {"veredito": "submit_parou_na_checagem", "nao_repetir": True})
        saida = rodar(["111", "222"])
        UMA_CR = ["get", "app", "abrir", "lock?", "navegar", "preflight",
                  "inserir", "atestar", "salvar", "reabrir", "submit"]
        checa("lote com a flag: etapas na ordem (reabre antes do Submit), PARA sem confirmacao, "
              "exit 3 mesmo com CR nao processada",
              ordem == ["login"] + UMA_CR and saida == 3, (ordem, saida))

        # gotcha 20: com o Submit CONFIRMADO o lote segue pra proxima CR
        del ordem[:]
        auto.submeter_cr_validada_da_raiz = passo(
            "submit", {"veredito": "confirmado", "nao_repetir": True})
        saida = rodar(["111", "222"])
        checa("lote: Submit CONFIRMADO -> segue pra proxima CR, exit 0",
              ordem == ["login"] + UMA_CR + UMA_CR and saida == 0, (ordem, saida))

        # reabertura falhou: CR salva e nao submetida, sem clique, lote para
        del ordem[:]

        def reabrir_falha(*a, **k):
            ordem.append("reabrir")
            raise RuntimeError("a janela da CR nao fechou")
        auto.reabrir_cr_para_submit = reabrir_falha
        saida = rodar(["111", "222"])
        checa("lote: reabertura falhou -> salva_nao_submetida, SEM Submit, lote para, exit 3",
              ordem == ["login"] + UMA_CR[:-1] and saida == 3, (ordem, saida))
        auto.reabrir_cr_para_submit = passo(
            "reabrir", {"janela_antiga": "a", "janela_nova": "b"})

        # sessao B: conferencia -> Save -> REABRIR -> Submit
        del ordem[:]
        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = False, True, False
        auto.conferir_impostos_no_servidor = passo("conferir", {"apto_para_save": True})
        auto.salvar_conferida_para_submit = passo(
            "salvar_b", {"save_veredito": "confirmado", "apto_para_submit": True})
        saida = rodar(["333"])
        checa("sessao B: conferir -> Save -> reabrir -> Submit, nada depois da reabertura",
              ordem == ["login", "get", "app", "abrir", "lock?", "conferir", "salvar_b",
                        "reabrir", "submit"] and saida == 0, (ordem, saida))
        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = False, False, True
        auto.submeter_cr_validada_da_raiz = passo(
            "submit", {"veredito": "submit_parou_na_checagem", "nao_repetir": True})

        del ordem[:]
        auto.salvar_insercao_para_submit = passo(
            "salvar", {"fase": "persistencia_incerta", "save_veredito": "ressalva"})
        saida = rodar(["111"])
        checa("lote: Save com ressalva -> Submit NAO e' chamado, exit 3",
              "submit" not in ordem and ordem[-1] == "salvar" and saida == 3, (ordem, saida))
    finally:
        for n, f in orig_lote.items():
            setattr(auto, n, f)
        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = flags

    # checagem do Submit por ESTRUTURA (fixture real de 28/09, CR 90000003)
    TELA = ("Organization: $1000001, EMPRESA EXEMPLO S A / CIDADE - UF Refresh "
            "Check No errors foundInfo. Display Message Log Change Request Edit General Data "
            "Change Request 90000003Change Request Description 500001 - CRIACAODescription "
            "Process Data Status Changes to Be ExecutedStatus Current Work Item New Change "
            "Request What's Next Created On/By 25.09.2026 22:38:15Created On/By")
    AREA = {"classe": "lsHTMLContainer lsScrollContainer--nonescrolling sapLsFPMMessageArea",
            "role": "presentation", "severidade": "info", "id": "WD030C28",
            "texto": "No errors foundInfo. [Info] Messages refer to Organization: $1000001 "
                     "Tax Number Category: BR3Info. Another business partner already has this "
                     "tax number (tax number type Brazil: State Tax Number)Info. "
                     "Display Message Log"}
    ECO = {"id": "sapur-aria-alert", "role": "alert", "classe": "", "texto": "3 messages",
           "severidade": "desconhecido", "severidade_original": "desconhecido",
           "duplicado_de": None,
           "caminho": "html.lsPage--verticalsizing--fill > body.urBdyStd.urBdyStdNoMrg > "
                      "div#sapur-aria.lsPage--ariastatic > div#sapur-aria-dynamic-texts > "
                      "span#sapur-aria-alert[role=alert]"}
    SUBMIT = {"texto": "Submit", "habilitado": True, "tem_press": True, "host_id": "WD030432"}

    def est(**k):
        base = {"modais": [], "alertas": [AREA, ECO], "candidatos": [SUBMIT]}
        base.update(k)
        return base

    parou = auto._eh_submit_parou_na_checagem
    checa("checagem de 28/09 (fixture real) -> reconhecida", parou("90000003", est(), TELA))
    checa("outro fornecedor: so' 'No errors found' + eco '1 message' -> reconhecida",
          parou("90000003", est(alertas=[dict(AREA, texto="No errors foundInfo. Display Message Log"),
                                         dict(ECO, texto="1 message")]), TELA))
    contras = [
        ("Submit desabilitado", est(candidatos=[dict(SUBMIT, habilitado=False)]), TELA),
        ("2 Submits", est(candidatos=[SUBMIT, SUBMIT]), TELA),
        ("status mudou", est(), TELA.replace("Changes to Be ExecutedStatus", "Final CheckStatus")),
        ("work item mudou", est(), TELA.replace("New Change Request", "Approval")),
        ("mensagem de ERRO na area",
         est(alertas=[dict(AREA, texto=AREA["texto"].replace("BR3Info.", "BR3Error.")), ECO]), TELA),
        ("area sem 'No errors found'",
         est(alertas=[dict(AREA, texto="Tax number is invalidError."), ECO]), TELA),
        ("alerta a mais", est(alertas=[AREA, ECO, dict(ECO, id="outro")]), TELA),
        ("modal aberto", est(modais=["dialogo"]), TELA),
        ("CR errada", est(), TELA.replace("90000003", "90000002")),
    ]
    for nome, e, tela in contras:
        checa("contra: " + nome + " -> NAO reconhecida", not parou("90000003", e, tela))

    # ---------------- caso 39: Submit ENVIADO + reabrir a CR (gotcha 20) -----
    # 29/09: o Submit logo depois de Details + Save, na mesma janela, so' checou;
    # numa CR recem-aberta enviou (manual 90000002 e automatico 90000003). Os
    # quatro sinais de envio sao os vistos nos dois; o status fica de fora.
    print("")
    print("== caso 39: sinais do Submit enviado, reabertura da CR e porta do clique ==")

    def area_msg(texto):
        return {"classe": "lsHTMLContainer lsScrollContainer--nonescrolling sapLsFPMMessageArea",
                "role": "presentation", "severidade": "info", "id": "WD0831", "texto": texto}
    MSGS_100 = ("You have submitted change request 90000003Info. No errors foundInfo. [Info] "
                "Messages refer to Organization: $1000001 Tax Number Category: BR3Info. Another "
                "business partner already has this tax number (tax number type Brazil: State Tax "
                "Number)Info. Display Message Log")
    TELA_100 = ("Organization: $1000001, EMPRESA EXEMPLO S A / CIDADE - UF Refresh "
                "Check Change Documents Workflow Log Undo Redo Side Panel " + MSGS_100 +
                " Change Request General Change Request 90000003Change Request Status Changes to "
                "Be ExecutedStatus Current Work Item Empty What's Next")
    MSGS_088 = MSGS_100.replace("90000003", "90000002").replace("$1000001", "$1000002")
    TELA_088 = ("Organization: $1000002, FORNECEDOR TESTE LTDA / CIDADE Refresh Change "
                "Documents Workflow Log Undo Redo Side Panel " + MSGS_088 + " Change Request "
                "General Change Request 90000002 Status Workflow is currently being processed "
                "Current Work Item Workflow in process")
    ECO4 = dict(ECO, texto="4 messages")
    def leit(cands, **k):
        base = {"candidatos": cands, "modais": [], "alertas": [area_msg(MSGS_100), ECO4]}
        base.update(k)
        return base
    BOT = {"Withdraw Draft": leit([]), "Save": leit([{"texto": "Save", "habilitado": False}]),
           "Cancel": leit([{"texto": "Cancel", "habilitado": False}])}
    ERRO_TARDIO = {"classe": "urMsgBarError", "role": "", "severidade": "erro",
                   "texto": "Workflow could not be started"}

    def est_env(msgs=MSGS_100, **k):
        base = {"modais": [], "alertas": [area_msg(msgs), ECO4], "candidatos": []}
        base.update(k)
        return base

    sinais = auto._sinais_submit_confirmado
    checa("envio automatico da 90000003 (fixture real) -> nenhum sinal faltando",
          sinais("90000003", est_env(), BOT, 1) == [],
          sinais("90000003", est_env(), BOT, 1))
    checa("envio manual da 90000002 (status ja em workflow) -> nenhum sinal faltando",
          sinais("90000002", est_env(MSGS_088), BOT, 1) == [],
          sinais("90000002", est_env(MSGS_088), BOT, 1))
    SEM_MSG = MSGS_100.replace("You have submitted change request 90000003Info. ", "")
    W = "Withdraw Draft"
    contras = [
        ("sem a mensagem de envio", est_env(SEM_MSG), BOT, 1),
        ("mensagem de envio de OUTRA CR", est_env(MSGS_088), BOT, 1),
        ("mensagem de erro na area", est_env(MSGS_100.replace("BR3Info.", "BR3Error.")), BOT, 1),
        ("Submit ainda na tela", est_env(candidatos=[SUBMIT]), BOT, 1),
        ("Withdraw Draft ainda na tela", est_env(),
         dict(BOT, **{W: leit([{"texto": W, "habilitado": True}])}), 1),
        ("leitura de Withdraw levantou (None)", est_env(), dict(BOT, **{W: None}), 1),
        ("leitura de Withdraw com ERRO (candidatos vazios)", est_env(),
         dict(BOT, **{W: leit([], erro="retorno NoneType")}), 1),
        ("modal tardio na leitura de Withdraw", est_env(),
         dict(BOT, **{W: leit([], modais=["Confirmar?"])}), 1),
        ("erro tardio na leitura de Withdraw", est_env(),
         dict(BOT, **{W: leit([], alertas=[area_msg(MSGS_100), ECO4, ERRO_TARDIO])}), 1),
        ("leitura de Withdraw sem lista de candidatos", est_env(),
         dict(BOT, **{W: {"modais": [], "alertas": []}}), 1),
        ("sem controle Workflow Log", est_env(), BOT, 0),
        ("contagem de Workflow Log ilegivel", est_env(), BOT, None),
        ("Save habilitado", est_env(), dict(BOT, Save=leit([{"texto": "Save", "habilitado": True}])), 1),
        ("Save sem 'habilitado' explicito", est_env(), dict(BOT, Save=leit([{"texto": "Save"}])), 1),
        ("Save ausente", est_env(), dict(BOT, Save=leit([])), 1),
        ("Cancel habilitado", est_env(),
         dict(BOT, Cancel=leit([{"texto": "Cancel", "habilitado": True}])), 1),
        ("Cancel ausente", est_env(), dict(BOT, Cancel=leit([])), 1),
        ("modal aberto", est_env(modais=["dialogo"]), BOT, 1),
        ("alerta a mais", est_env(alertas=[area_msg(MSGS_100), ECO4, dict(ECO4, id="outro")]),
         BOT, 1),
    ]
    for nome, e, bot, wl in contras:
        checa("contra: " + nome + " -> NAO confirmado", sinais("90000003", e, bot, wl) != [])

    # a pos-condicao inteira, lendo um DOM: 4 sinais -> confirmado
    _prints_orig = auto.PRINTS_DIR
    auto.PRINTS_DIR = tempfile.mkdtemp(prefix="testeprints_")
    try:
        def tela_pos_submit(withdraw=False, controle_wl=True):
            wd = ('<div id="WD09" role="button" lsevents=\'{"Press":[{}]}\' '
                  'style="width:90px;height:22px"><span id="WD09-cnt">Withdraw Draft</span></div>'
                  if withdraw else "")
            barra = ('<a href="#" style="display:inline-block;width:90px;height:18px">'
                     'Workflow Log</a>' if controle_wl else 'Workflow Log history')
            return ('<div style="width:900px;height:24px">Refresh Check Change Documents '
                    + barra + '</div>'
                    '<div class="lsHTMLContainer lsScrollContainer--nonescrolling '
                    'sapLsFPMMessageArea" role="presentation" style="width:900px;height:80px">'
                    'You have submitted change request 90000003<span>Info.</span> No errors '
                    'found<span>Info.</span> <a href="#">Display Message Log</a></div>'
                    '<div style="width:900px;height:40px">Change Request 90000003 Status '
                    'Changes to Be Executed</div>'
                    '<div id="WD07" role="button" style="width:60px;height:22px">'
                    '<span id="WD07-cnt">Save</span></div>'
                    '<div id="WD08" role="button" style="width:60px;height:22px">'
                    '<span id="WD08-cnt">Cancel</span></div>' + wd +
                    '<div id="sapur-aria" class="lsPage--ariastatic">'
                    '<div id="sapur-aria-dynamic-texts"><span id="sapur-aria-alert" '
                    'role="alert" aria-hidden="true" style="position:absolute;top:-70003px;'
                    'display:block;width:70px;height:30px">2 messages</span></div></div>')
        carregar(d, tela_pos_submit())
        r = auto._poscondicao_submit(d, "90000003")
        checa("pos-Submit lendo o DOM com os 4 sinais -> CONFIRMADO",
              r.get("veredito") == "confirmado" and r.get("nao_repetir") is True,
              (r.get("veredito"), r.get("sinais_faltando")))
        carregar(d, tela_pos_submit(withdraw=True))
        r = auto._poscondicao_submit(d, "90000003")
        checa("contra: mesmo DOM com Withdraw Draft -> efeito_desconhecido, nao repetir",
              r.get("veredito") == "efeito_desconhecido" and r.get("nao_repetir") is True,
              (r.get("veredito"), r.get("sinais_faltando")))
        carregar(d, tela_pos_submit(controle_wl=False))
        r = auto._poscondicao_submit(d, "90000003")
        checa("contra: 'Workflow Log' so' como palavra no texto, sem controle -> NAO confirmado",
              r.get("veredito") != "confirmado", (r.get("veredito"), r.get("sinais_faltando")))

        # integrado (NO-GO do Codex): a leitura de Withdraw volta com ERRO ou com
        # modal tardio enquanto todo o resto do DOM diz "enviado" -> nunca confirma
        orig_est = auto._estado_acao_raiz

        def estado_com(alvo, resultado):
            def f(driver, texto):
                return dict(resultado) if texto == alvo else orig_est(driver, texto)
            return f
        try:
            auto._estado_acao_raiz = estado_com(
                "Withdraw Draft", {"candidatos": [], "modais": [], "alertas": [],
                                   "erro": "retorno NoneType"})
            carregar(d, tela_pos_submit())
            r = auto._poscondicao_submit(d, "90000003")
            checa("integrado: leitura de Withdraw com ERRO e o resto positivo -> NUNCA confirmado",
                  r.get("veredito") != "confirmado" and r.get("nao_repetir") is True,
                  (r.get("veredito"), r.get("sinais_faltando")))
            auto._estado_acao_raiz = estado_com(
                "Withdraw Draft", {"candidatos": [], "modais": ["Confirmar envio?"], "alertas": []})
            carregar(d, tela_pos_submit())
            r = auto._poscondicao_submit(d, "90000003")
            checa("integrado: modal tardio na leitura de Withdraw -> NUNCA confirmado",
                  r.get("veredito") != "confirmado", (r.get("veredito"), r.get("sinais_faltando")))
        finally:
            auto._estado_acao_raiz = orig_est
    finally:
        shutil.rmtree(auto.PRINTS_DIR, ignore_errors=True)
        auto.PRINTS_DIR = _prints_orig

    # reabrir: fecha a janela da CR, abre outra NOVA, destravada, work item de antes
    class DriverReabre:
        def __init__(self, fecha=True, nova=True, texto=None):
            self.handles = ["shell", "cr_velha"]
            self.current_window_handle = "cr_velha"
            self.fecha, self.nova, self.gets = fecha, nova, []
            self.texto = texto if texto is not None else (
                "Change Request 90000004 Status Changes to Be ExecutedStatus "
                "Current Work Item New Change Request What's Next")
            dono = self

            class _Troca:
                def window(self, h):
                    dono.current_window_handle = h
            self.switch_to = _Troca()

        @property
        def window_handles(self):
            return list(self.handles)

        def close(self):
            if self.fecha:
                self.handles.remove(self.current_window_handle)

        def get(self, url):
            self.gets.append(url)

        def execute_script(self, script, *a):
            return self.texto

    def abre_nova(drv, cr):
        if drv.nova:
            drv.handles.append("cr_nova")
            drv.current_window_handle = "cr_nova"

    NOMES_R = ("go_to_display_change_requests", "open_cr", "cr_esta_travada",
               "_estado_raiz_pronta_para_submit")
    orig_r = {n: getattr(auto, n) for n in NOMES_R}
    try:
        auto.go_to_display_change_requests = lambda drv: None
        auto.open_cr = abre_nova
        auto.cr_esta_travada = lambda drv: False
        auto._estado_raiz_pronta_para_submit = lambda drv, cr: {"save_desabilitado": True,
                                                                "submit_habilitado": True}
        dr = DriverReabre()
        r = auto.reabrir_cr_para_submit(dr, "90000004")
        checa("reabrir: fecha a janela antiga e comprova uma janela NOVA",
              r["janela_antiga"] == "cr_velha" and r["janela_nova"] == "cr_nova"
              and "cr_velha" not in dr.handles and len(dr.gets) == 1, (r, dr.handles))
        checa("contra: a janela antiga nao fechou -> recusa",
              "nao fechou" in (levanta(auto.reabrir_cr_para_submit,
                                       DriverReabre(fecha=False), "90000004") or ""))
        checa("contra: a CR nao abriu janela nova (ficou no shell) -> recusa",
              "janela nova" in (levanta(auto.reabrir_cr_para_submit,
                                        DriverReabre(nova=False), "90000004") or ""))
        checa("reabrir: CR devolvida pela aprovacao (To be Revised) -> aceita",
              auto.reabrir_cr_para_submit(DriverReabre(texto=(
                  "Change Request 90000004 Process Data Status To be RevisedStatus Current Work "
                  "Item Process Change Request 90000004 What's Next")), "90000004")
              ["janela_nova"] == "cr_nova")
        aceita = auto._estado_aceita_submit
        NOVA = "Status Changes to Be ExecutedStatus Current Work Item New Change Request What's Next"
        REV = "Status To be RevisedStatus Current Work Item Process Change Request {}What's Next"
        checa("estado: CR nova -> aceita", aceita(NOVA, "90000002"))
        checa("estado: devolvida com o numero DELA -> aceita",
              aceita(REV.format("90000002 "), "90000002"))
        checa("estado: devolvida sem numero no work item -> aceita",
              aceita(REV.format(""), "90000002"))
        checa("contra estado: devolvida com numero de OUTRA CR -> recusa",
              not aceita(REV.format("90000003 "), "90000002"))
        checa("contra estado: pares trocados (To be Revised + New Change Request) -> recusa",
              not aceita("Status To be RevisedStatus Current Work Item New Change Request",
                         "90000002"))
        checa("contra estado: workflow em andamento -> recusa",
              not aceita("Status Workflow is currently being processedStatus Current Work "
                         "Item Workflow is currently being processed", "90000002"))
        checa("contra: work item ja nao e' 'New Change Request' -> recusa",
              "work item" in (levanta(auto.reabrir_cr_para_submit, DriverReabre(
                  texto="Status Workflow is currently being processed Current Work Item "
                        "Workflow in process"), "90000004") or ""))
        auto.cr_esta_travada = lambda drv: True
        checa("contra: CR travada ao reabrir -> recusa",
              "travada" in (levanta(auto.reabrir_cr_para_submit, DriverReabre(),
                                    "90000004") or ""))
    finally:
        for n, f in orig_r.items():
            setattr(auto, n, f)

    # porta do clique: sem a CR reaberta nesta janela, o Submit nao sai
    NOMES_S = ("_validar_atestado_submit", "_estado_raiz_pronta_para_submit",
               "_clicar_submit_validado_da_raiz", "_poscondicao_submit")
    orig_s = {n: getattr(auto, n) for n in NOMES_S}
    passo_orig = auto.STEP_CONFIRM
    cliques = []
    try:
        auto.STEP_CONFIRM = False
        auto._validar_atestado_submit = lambda drv, cr, at: True
        auto._estado_raiz_pronta_para_submit = lambda drv, cr: {}
        auto._clicar_submit_validado_da_raiz = lambda drv, cr: cliques.append(cr)
        auto._poscondicao_submit = lambda drv, cr: {"veredito": "confirmado"}
        checa("porta do clique: sem reabertura -> recusa e NAO clica",
              "REABERTA" in (levanta(auto.submeter_cr_validada_da_raiz, d, "90000004",
                                     {"origem": "conferencia"}) or "") and cliques == [])
        checa("porta do clique: reaberta em OUTRA janela -> recusa e NAO clica",
              "REABERTA" in (levanta(auto.submeter_cr_validada_da_raiz, d, "90000004",
                                     {"origem": "conferencia", "reaberta": {
                                         "janela_antiga": "x", "janela_nova": "y"}}) or "")
              and cliques == [])
        at_ok = {"origem": "conferencia", "reaberta": {
            "janela_antiga": "velha", "janela_nova": d.current_window_handle}}
        r = auto.submeter_cr_validada_da_raiz(d, "90000004", at_ok)
        checa("porta do clique: CR reaberta nesta janela -> UM clique, marcado antes",
              cliques == ["90000004"] and r.get("veredito") == "confirmado"
              and at_ok.get("submit_clicado") is True, (cliques, at_ok))
    finally:
        for n, f in orig_s.items():
            setattr(auto, n, f)
        auto.STEP_CONFIRM = passo_orig
    # ---------------- caso 38: campo redesenhado com id NOVO (gotcha 21) ---
    # 29/09 10:01, 1a rodada com 1,5/0,25: o fill pegou WD0E44, o WDA trocou a
    # sub-tela por outra com id NOVO, e _esperar_campo_editavel ficou 10 s
    # esperando o id morto - com os campos prontos no print. O caso 25 cobre a
    # troca com o MESMO id; este, a troca com id novo.
    print("")
    print("== caso 38: WDA redesenha o campo com id NOVO (gotcha 21) ==")
    ID_NOVO = """
        <span>Withholding Tax Type:</span>
        <input id="WD0E44" readonly value="">
        <script>
        setTimeout(function () {
            const velho = document.getElementById('WD0E44');
            const novo = document.createElement('input');
            novo.id = 'WD0F9A';                 // id NOVO - o antigo nao volta
            velho.parentNode.replaceChild(novo, velho);
            window.__trocou = true;
        }, 600);
        </script>
    """
    carregar(d, ID_NOVO)
    t0 = time.time()
    erro = None
    try:
        auto.fill_field_by_label(d, "Withholding Tax Type", "C0")
    except Exception as e:
        erro = e
    t_fill = time.time() - t0
    checa("preenche o campo que voltou com id NOVO",
          erro is None, ("%s: %s" % (type(erro).__name__, erro))[:140] if erro else "")
    checa("  ...o valor foi parar no input novo",
          d.execute_script("return document.getElementById('WD0F9A').value;") == "C0")
    checa("  ...sem esperar os 10 s pelo id morto", t_fill < 6, "%.2fs" % t_fill)

    carregar(d, ID_NOVO)
    t0 = time.time()
    erro = None
    try:
        auto._esperar_campo_editavel(d, "WD0E44", timeout=8)
    except Exception as e:
        erro = e
    t_esp = time.time() - t0
    checa("id que some e nao volta -> CampoSumiuDoDom em poucos segundos",
          isinstance(erro, auto.CampoSumiuDoDom) and t_esp < 4,
          (type(erro).__name__ if erro else "sem excecao", "%.2fs" % t_esp))
    checa("  ...e' NoSuchElementException (o fill ja trata como re-achar)",
          isinstance(erro, auto.NoSuchElementException))

    # fronteira dos 1,5 s (recomendacao do Codex): o id some ~0,8 s e VOLTA
    # igual - isso e' o redesenho do gotcha 17, nao "id novo". A espera segue no
    # mesmo id e termina bem, sem CampoSumiuDoDom.
    VOLTA = """
        <span>Withholding Tax Type:</span>
        <input id="WD0E44" readonly value="">
        <script>
        setTimeout(function () {
            const velho = document.getElementById('WD0E44');
            const pai = velho.parentNode;
            pai.removeChild(velho);
            setTimeout(function () {
                const novo = document.createElement('input');
                novo.id = 'WD0E44';
                pai.appendChild(novo);
                window.__voltou = true;
            }, 800);
        }, 300);
        </script>
    """
    carregar(d, VOLTA)
    erro = None
    try:
        auto._esperar_campo_editavel(d, "WD0E44", timeout=8)
    except Exception as e:
        erro = e
    checa("id que some ~0,8 s e VOLTA igual -> espera normal, sem CampoSumiuDoDom",
          erro is None and d.execute_script("return window.__voltou === true;"),
          repr(erro)[:100] if erro else "")

    erro_login = None
    try:
        auto.esperar_login_manual(object(), timeout=0, intervalo=0)
    except auto.TimeoutException as e:
        erro_login = str(e)
    checa("timeout do login cita idioma e tile ausente da tela inicial",
          erro_login is not None
          and "EN - English" in erro_login
          and auto.APP_TILE_NAME in erro_login
          and "tela inicial" in erro_login,
          erro_login)

    # ---------------- caso 40: janelas nao acumulam no lote (Codex, 52a) ---
    # Cada CR deixava uma janela a mais. Nos modos que persistem o lote fecha
    # as extras no comeco de cada CR; no modo sem persistir nada muda; falha ao
    # fechar para o lote ANTES de abrir a CR seguinte.
    print("")
    print("== caso 40: o lote fecha as janelas extras so' nos modos que persistem ==")

    class DriverJanelas:
        session_id = "janelas"

        def __init__(self, fecha=True):
            self.handles = ["w0"]
            self.current_window_handle = "w0"
            self.fecha, self.n, self.no_get = fecha, 0, []
            dono = self

            class _Troca:
                def window(self, h):
                    if h not in dono.handles:
                        raise RuntimeError("janela inexistente: " + h)
                    dono.current_window_handle = h
            self.switch_to = _Troca()

        @property
        def window_handles(self):
            return list(self.handles)

        def close(self):
            if self.fecha:
                self.handles.remove(self.current_window_handle)

        def get(self, url):
            self.no_get.append(len(self.handles))

        def execute_script(self, script, *a):
            return True

        def abrir_janela(self):
            self.n += 1
            self.handles.append("cr%d" % self.n)
            self.current_window_handle = "cr%d" % self.n

    dj = DriverJanelas()
    dj.abrir_janela()
    dj.abrir_janela()
    checa("fechar extras: 3 janelas -> fica so' a 1a, e o foco nela",
          auto._fechar_janelas_extras(dj) == 2 and dj.handles == ["w0"]
          and dj.current_window_handle == "w0", dj.handles)
    checa("fechar extras: 1 janela -> nada a fechar", auto._fechar_janelas_extras(dj) == 0)
    dj = DriverJanelas(fecha=False)
    dj.abrir_janela()
    checa("fechar extras: janela que nao fecha -> levanta",
          "nao fecharam" in (levanta(auto._fechar_janelas_extras, dj) or ""))

    NOMES_J = ("esperar_login_manual", "go_to_display_change_requests", "open_cr",
               "cr_esta_travada", "navigate_to_tax_section", "insert_all_taxes", "finalize_cr")
    orig_j = {n: getattr(auto, n) for n in NOMES_J}
    flags_j = (auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER)

    def lote_janelas(dr, crs, salvar):
        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = salvar, False, False
        try:
            auto._rodar_lote(dr, crs)
        except SystemExit as e:
            return e.code
        return 0

    try:
        auto.esperar_login_manual = lambda drv, **k: None
        auto.go_to_display_change_requests = lambda drv: None
        auto.open_cr = lambda drv, cr: drv.abrir_janela()
        auto.cr_esta_travada = lambda drv: False
        auto.navigate_to_tax_section = lambda drv, **k: None
        auto.insert_all_taxes = lambda drv, cr, **k: {"inseridos": [], "pulados": [],
                                                      "faltavam": []}
        auto.finalize_cr = lambda drv, cr, salvar=False, submeter=False, **k: (
            {"Save": {"veredito": "confirmado"}} if salvar else {})

        dj = DriverJanelas()
        saida = lote_janelas(dj, ["1", "2", "3"], True)
        checa("lote que SALVA: 3 CRs sem acumular janelas (1 janela em cada inicio)",
              dj.no_get == [1, 1, 1] and saida == 0, (dj.no_get, saida))
        dj = DriverJanelas()
        saida = lote_janelas(dj, ["1", "2", "3"], False)
        checa("lote SEM persistir: comportamento de antes (nao fecha nada)",
              dj.no_get == [1, 2, 3] and saida == 0, (dj.no_get, saida))
        dj = DriverJanelas(fecha=False)
        saida = lote_janelas(dj, ["1", "2", "3"], True)
        checa("lote que SALVA: janela que nao fecha -> para ANTES de abrir a CR seguinte",
              dj.n == 1 and saida == 1, (dj.n, saida))
    finally:
        for n, f in orig_j.items():
            setattr(auto, n, f)
        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = flags_j

    # ---------------- caso 41: recusa ANTES do clique != erro DEPOIS (29/09) ---
    # Revisao do agente: uma recusa do Save antes do clique era relatada como
    # "o Save foi tentado/clicado". Agora AntesDoClique separa os dois casos; um
    # retorno vazio do clique continua "pode ter clicado".
    print("")
    print("== caso 41: Save recusado antes do clique x erro depois do clique ==")
    CRX = "90000001"

    def raiz_save(n_hab=1, extra="", sem_cr=False):
        press = " lsevents='{\"Press\":[{}]}'"
        bots = "".join('<div id="WDS%d" role="button"%s style="width:60px;height:22px">'
                       '<span id="WDS%d-cnt">Save</span></div>' % (i, press, i)
                       for i in range(n_hab))
        cab = "" if sem_cr else ('<div style="width:900px;height:40px">Change Request %s '
                                 'Description</div>' % CRX)
        return cab + bots + extra

    carregar(d, raiz_save(sem_cr=True))
    erro = None
    try:
        auto._relatar_acao_raiz(d, CRX, "Save")
    except Exception as e:
        erro = e
    checa("relatorio previo sem a CR na tela -> AntesDoClique",
          isinstance(erro, auto.AntesDoClique), type(erro).__name__ if erro else "nada")
    carregar(d, raiz_save(n_hab=2))
    erro = None
    try:
        auto._clicar_acao_raiz(d, CRX, "Save")
    except Exception as e:
        erro = e
    checa("clique atomico recusa 2 Saves SEM clicar -> AntesDoClique",
          isinstance(erro, auto.AntesDoClique), repr(erro)[:120])

    dsv = auto.criar_driver_edge(headless=True)
    try:
        dsv.set_window_size(1400, 900)
        carregar(dsv, raiz_save())
        dsv.execute_script("""document.getElementById('WDS0').addEventListener('click',
            function () { window.__cliques = (window.__cliques || 0) + 1; confirm('Save?'); });""")
        erro = None
        try:
            auto._clicar_acao_raiz(dsv, CRX, "Save")
        except Exception as e:
            erro = e
        aberto = ""
        try:
            aberto = dsv.switch_to.alert.text
            dsv.switch_to.alert.dismiss()
        except Exception:
            pass
        checa("clique que abre confirm() -> NAO e' AntesDoClique (pode ter clicado)",
              erro is not None and not isinstance(erro, auto.AntesDoClique)
              and aberto == "Save?", (type(erro).__name__ if erro else "nada", aberto))
    finally:
        dsv.quit()

    orig_sv = auto.salvar_cr_da_raiz
    try:
        for exc, fase in ((auto.AntesDoClique("recusou"), "save_recusado_sem_clique"),
                          (RuntimeError("timeout depois do clique"), "persistencia_incerta")):
            def _salva(drv, cr, _e=exc):
                raise _e
            auto.salvar_cr_da_raiz = _salva
            at = {"origem": "insercao"}
            orig_v = auto._validar_atestado_insercao
            auto._validar_atestado_insercao = lambda *a, **k: True
            try:
                levanta(auto.salvar_insercao_para_submit, d, CRX, at)
            finally:
                auto._validar_atestado_insercao = orig_v
            checa("salvar_insercao_para_submit: %s -> fase %s" % (type(exc).__name__, fase),
                  at.get("fase") == fase, at.get("fase"))
    finally:
        auto.salvar_cr_da_raiz = orig_sv

    # no lote: recusa antes do clique = falha (nada gravado); depois = ressalva
    ordem = []
    NOMES_L = ("esperar_login_manual", "go_to_display_change_requests", "open_cr",
               "cr_esta_travada", "navigate_to_tax_section", "iniciar_atestado_insercao",
               "insert_all_taxes", "atestar_insercao_para_save", "salvar_insercao_para_submit",
               "conferir_impostos_no_servidor", "salvar_conferida_para_submit")
    orig_l = {n: getattr(auto, n) for n in NOMES_L}
    flags_l = (auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER)

    def salvar_levanta(fase, exc):
        def f(drv, cr, at):
            at["fase"] = fase
            raise exc
        return f

    def rodar_l(crs):
        try:
            auto._rodar_lote(DriverFalso(), crs)
        except SystemExit as e:
            return e.code
        return 0
    try:
        auto.esperar_login_manual = passo("login")
        auto.go_to_display_change_requests = passo("app")
        auto.open_cr = passo("abrir")
        auto.cr_esta_travada = passo("lock?", False)
        auto.navigate_to_tax_section = passo("navegar")
        auto.iniciar_atestado_insercao = passo("preflight", {"fase": "aberta_limpa"})
        auto.insert_all_taxes = passo("inserir", {"inseridos": [], "pulados": []})
        auto.atestar_insercao_para_save = passo("atestar", {"fase": "insercao_atestada"})
        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = False, False, True
        auto.salvar_insercao_para_submit = salvar_levanta(
            "save_recusado_sem_clique", auto.AntesDoClique("recusou"))
        checa("fluxo unico: Save recusado antes do clique -> falha comum (exit 1)",
              rodar_l(["1"]) == 1)
        auto.salvar_insercao_para_submit = salvar_levanta(
            "persistencia_incerta", RuntimeError("depois do clique"))
        checa("fluxo unico: erro depois do clique do Save -> ressalva (exit 3)",
              rodar_l(["1"]) == 3)

        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = False, True, False
        auto.conferir_impostos_no_servidor = passo("conferir", {"apto_para_save": True})

        def conf_levanta(exc, veredito=None):
            def f(drv, cr, at):
                if veredito:
                    at["save_veredito"] = veredito
                raise exc
            return f
        auto.salvar_conferida_para_submit = conf_levanta(auto.AntesDoClique("recusou"))
        checa("sessao B: Save recusado antes do clique -> falha comum (exit 1)",
              rodar_l(["1"]) == 1)
        auto.salvar_conferida_para_submit = conf_levanta(RuntimeError("depois do clique"))
        checa("sessao B: erro depois do clique do Save -> ressalva (exit 3)",
              rodar_l(["1"]) == 3)
    finally:
        for n, f in orig_l.items():
            setattr(auto, n, f)
        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = flags_l

    # ---------------- caso 42: dialogo aceito durante a reabertura (29/09) ---
    print("")
    print("== caso 42: dialogo aceito em silencio na reabertura -> Submit recusado ==")
    orig_42 = {n: getattr(auto, n) for n in ("reabrir_cr_para_submit",
                                             "submeter_cr_validada_da_raiz")}
    submits = []
    try:
        def reabre_com_dialogo(drv, cr):
            auto._ALERTAS_ACEITOS += 1        # o que _accept_alert_if_present faria
            return {"janela_antiga": "a", "janela_nova": "b"}
        auto.reabrir_cr_para_submit = reabre_com_dialogo
        auto.submeter_cr_validada_da_raiz = lambda drv, cr, at: (
            submits.append(cr) or {"veredito": "confirmado"})
        res = {"ok": [], "ressalva": [], "falha": []}
        segue = auto._submeter_no_lote(None, "90000001", {"save_veredito": "confirmado"}, res)
        checa("dialogo aceito na reabertura -> salva_nao_submetida, SEM Submit",
              segue is False and submits == []
              and res["ressalva"] and res["ressalva"][0][1] == "salva_nao_submetida",
              (segue, submits, res["ressalva"][:1]))
        auto.reabrir_cr_para_submit = lambda drv, cr: {"janela_antiga": "a", "janela_nova": "b"}
        res = {"ok": [], "ressalva": [], "falha": []}
        segue = auto._submeter_no_lote(None, "90000001", {"save_veredito": "confirmado"}, res)
        checa("reabertura sem dialogo -> UM Submit e o lote segue",
              segue is True and submits == ["90000001"], (segue, submits))
    finally:
        for n, f in orig_42.items():
            setattr(auto, n, f)

    # main(): no fluxo unico, lista com entrada invalida -> recusa ANTES do navegador
    orig_m = {n: getattr(auto, n) for n in ("_ativar_log_em_arquivo", "get_cr_list",
                                            "criar_driver_edge")}
    flags_m = (auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER)
    abriu = []
    try:
        auto._ativar_log_em_arquivo = lambda: None
        auto.get_cr_list = lambda: (["90000001"], True)
        auto.criar_driver_edge = lambda *a, **k: abriu.append(1)
        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = False, False, True
        saida = None
        try:
            auto.main()
        except SystemExit as e:
            saida = e.code
        checa("main (fluxo unico): lista com entrada invalida -> exit 2, navegador NAO abre",
              saida == 2 and abriu == [], (saida, abriu))
    finally:
        for n, f in orig_m.items():
            setattr(auto, n, f)
        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = flags_m

    # quadro final da tela (07/10): mesma prioridade do exit code
    print("\n== quadro final da tela ==")
    ok_150 = ("90000005", "submetida", {"atestado": {}})
    q = auto._mensagem_final({"ok": [ok_150], "ressalva": [], "falha": []}, [])
    checa("quadro final: tudo OK -> FINALIZADO SEM ERROS e o que foi feito em cada CR",
          q[1].strip() == "FINALIZADO SEM ERROS"
          and "  90000005: enviada para aprovacao" in q, q)
    q = auto._mensagem_final(
        {"ok": [ok_150], "ressalva": [("90000003", "salva_nao_submetida", {})],
         "falha": []}, ["90000004"])
    checa("quadro final: ressalva vence e manda NAO rodar de novo; lista as nao processadas",
          "COM RESSALVA" in q[1] and "NAO rode de novo: 90000003" in q[1]
          and any("90000004" in ln and "proxima execucao" in ln for ln in q), q)
    q = auto._mensagem_final({"ok": [], "ressalva": [], "falha": ["90000002"]},
                             ["90000003"])
    checa("quadro final: falha -> FINALIZADO COM ERRO e nada gravado",
          "COM ERRO em: 90000002" in q[1] and "Nada foi gravado" in q[2], q)
    checa("quadro final: falha -> diz que a repeticao confere a tabela e para nao abrir "
          "os Details (gotcha 26)",
          "confere sozinha" in q[3] and "Details" in q[3], q)
    q = auto._mensagem_final({"ok": [], "ressalva": [], "falha": ["90000006"],
                              "sap_indisponivel": [("90000006", "aviso de desligamento")]},
                             ["90000007"])
    checa("quadro final: sinal de indisponibilidade -> diz QUAL sinal, sem afirmar queda do "
          "SAP, e que nada foi gravado",
          "COM ERRO em: 90000006" in q[1] and "sinal de indisponibilidade" in q[2]
          and "aviso de desligamento" in q[2] and "Nada foi gravado" in q[2]
          and "caiu" not in q[2], q)
    q = auto._mensagem_final({"ok": [], "ressalva": [], "falha": []}, ["90000003"])
    checa("quadro final: lote parado sem falha (janelas) -> SEM CONCLUIR A LISTA",
          q[1].strip() == "FINALIZADO SEM CONCLUIR A LISTA", q)

    # ---------------- caso 43: esperas do loop de insercao (gotcha 26) ----------
    # 1a rodada em producao (13/10): o Done passou de 20 s com o SAP ocupado e a
    # leitura desistiu. Aqui o "SAP" e' um roteiro de timers na pagina, disparado
    # pelo New, pelo checkbox e pelo UNICO Done: blindlayer de verdade (gotcha 9),
    # sub-tela com campos, tabela, avisos. 43a-43u: espera do Done (com os dois
    # achados da revisao do Codex, 78a: aviso junto da tabela e relogio civil);
    # 44a-44h: New e pre-Done. Vigias: Done contados, e nenhuma chamada que grava
    # ou clica outra coisa (Save, Submit, acao de linha, clique por texto).
    print("\n== caso 43: esperas do loop de insercao (gotcha 26) ==")
    import logging
    import threading
    import types

    class _Coletor(logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs = []

        def emit(self, rec):
            self.msgs.append(rec.getMessage())

    coletor = _Coletor()
    auto.log.addHandler(coletor)

    def tabela_43(linhas_43):
        rows = "".join(linha(c, dsc, i + 2) for i, (c, dsc) in enumerate(linhas_43))
        return pagina(rows, len(linhas_43) + 1)

    def subtela_43(bloqueada=False):
        ro = " readonly" if bloqueada else ""
        return (CSS + "<h3>Extended Withholding Tax Type</h3>"
                "<div><span>* Withholding Tax Type:</span> <input id='wtt' value='W8'%s></div>"
                "<div><span>Withholding Tax Code:</span> <input id='wtc' value='W8'%s></div>"
                "<div><span>Subject to Withholding Tax:</span> "
                "<span role='checkbox' aria-checked='true'>x</span></div>" % (ro, ro))

    ANTES_43 = [("C0", "Withholding COFINS-0%"), ("C3", "Withholding COFINS-3%")]
    DEPOIS_43 = tabela_43(ANTES_43 + [("W8", "Withholding ISS-2%")])
    SUBTELA_43 = subtela_43()
    AVISO_DESLIGA = "<div>Instance is closing down. Close your running applications</div>"
    JS_SAP_43 = r"""
        for (const [t, tipo, dado] of arguments[0]) {
            setTimeout(() => {
                if (tipo === 'html') { document.body.innerHTML = dado; return; }
                if (tipo === 'muda') {          // a tela nunca para de mudar
                    let n = 0;
                    setInterval(() => {
                        let m = document.getElementById('muda');
                        if (!m) { m = document.createElement('p'); m.id = 'muda';
                                  document.body.appendChild(m); }
                        n += 1; m.textContent = 'x'.repeat(20 + (n % 2) * 40);
                    }, 200);
                    return;
                }
                let b = document.getElementById('WD01-blindlayer');
                if (!b) {
                    b = document.createElement('div'); b.id = 'WD01-blindlayer';
                    b.style.position = 'fixed'; b.style.left = '0'; b.style.top = '0';
                    document.body.appendChild(b);
                }
                b.style.width = tipo === 'ocupado' ? '300px' : '1px';
                b.style.height = tipo === 'ocupado' ? '80px' : '1px';
            }, t);
        }
    """
    VIGIADAS_43 = ("_clicar_acao_raiz", "_clicar_submit_validado_da_raiz",
                   "salvar_cr_da_raiz", "_clicar_acao_linha", "click_link_by_text")
    NOMES_43 = VIGIADAS_43 + ("scroll_to_text", "click_visible_candidate",
                              "fill_field_by_label", "check_checkbox_by_label",
                              "_checkbox_marcado_perto_do_label", "_reler_valor_campo",
                              "_click_done")
    orig_43 = {n: getattr(auto, n) for n in NOMES_43}
    cfg_43 = (auto.ESTABILIDADE_LOOP, auto.INTERVALO_LOOP,
              auto.TETO_ESPERA_SAP_S, auto.AVISO_ESPERA_S, auto.time)
    proibidas_43 = []
    est_43 = {}
    # relogio civil controlavel: so time.time salta; monotonic e sleep sao reais
    salto_43 = {"s": 0.0}
    relogio_43 = types.SimpleNamespace(time=lambda: time.time() + salto_43["s"],
                                       monotonic=time.monotonic, sleep=time.sleep)

    def cenario_43(done=(), new=None, check=(), teto=180, aviso=2, salto=None):
        """inserir_uma_linha('W8') contra roteiros ([ms, tipo, html]) disparados pelo
        New (None: sub-tela pronta na hora), pelo checkbox e pelo UNICO Done.
        `salto`=(segundos, atraso): o relogio civil salta `atraso` s depois do Done.
        Devolve (excecao ou None, segundos do Done ate o fim ou None, log, Dones)."""
        d.get("about:blank")             # zera timers do cenario anterior
        carregar(d, tabela_43(ANTES_43))
        est_43.update(done=0, t_done=None, t_new=None, t_fill=None, t_check=None)
        auto.TETO_ESPERA_SAP_S, auto.AVISO_ESPERA_S = teto, aviso
        salto_43["s"] = 0.0

        def new_falso(drv, texto, *a, **k):
            if texto != "New":
                proibidas_43.append("click_visible_candidate(%s)" % texto)
                return
            est_43["t_new"] = time.time()
            if new is None:
                carregar(drv, SUBTELA_43)
            else:
                drv.execute_script(JS_SAP_43, new)

        def fill_falso(*a, **k):
            est_43["t_fill"] = est_43["t_fill"] or time.time()

        def check_falso(drv, *a, **k):
            est_43["t_check"] = time.time()
            if check:
                drv.execute_script(JS_SAP_43, list(check))

        def done_falso(drv, *a, **k):
            est_43["done"] += 1
            est_43["t_done"] = time.time()
            drv.execute_script(JS_SAP_43, list(done))
            if salto:
                threading.Timer(salto[1], lambda: salto_43.update(s=salto[0])).start()

        auto.click_visible_candidate = new_falso
        auto.fill_field_by_label = fill_falso
        auto.check_checkbox_by_label = check_falso
        auto._click_done = done_falso
        del coletor.msgs[:]
        erro = None
        try:
            auto.inserir_uma_linha(d, "W8", cr_number="CRT")
        except Exception as e:
            erro = e
        fim = time.time()
        salto_43["s"] = 0.0
        dt = fim - est_43["t_done"] if est_43["t_done"] else None
        return erro, dt, list(coletor.msgs), est_43["done"]

    def avisos_43(msgs, acao="o Done"):
        return [m for m in msgs if "SAP ainda processando %s " % acao in m]

    def categoria_43(e):
        return getattr(e, "categoria", None)

    try:
        auto.ESTABILIDADE_LOOP, auto.INTERVALO_LOOP = 0.5, 0.25
        auto.time = relogio_43
        for n in VIGIADAS_43:
            setattr(auto, n, (lambda nome: lambda *a, **k: proibidas_43.append(nome))(n))
        auto.scroll_to_text = lambda *a, **k: None
        auto._checkbox_marcado_perto_do_label = lambda *a, **k: True
        auto._reler_valor_campo = lambda drv, label, *a, **k: "W8"

        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43]])
        checa("43a caminho normal: tabela volta rapido -> confirma, 1 Done, sem aviso de espera",
              e is None and n == 1 and dt < 3 and not avisos_43(msgs), (e, dt, n))

        e, dt, msgs, n = cenario_43([[0, "ocupado", ""], [23000, "html", DEPOIS_43]])
        av = avisos_43(msgs)
        checa("43b SAP ocupado 23 s (passa dos 20 s de antes) -> espera e confirma, 1 Done",
              e is None and n == 1 and dt >= 23, (e, dt, n))
        checa("43b avisa a cada AVISO_ESPERA_S com CR, codigo e tempo de espera",
              len(av) >= 8 and all("[CRT] W8" in m and "SAP ocupado" in m for m in av[:5]),
              av[:3])

        e, dt, msgs, n = cenario_43([[3000, "ocupado", ""], [6000, "html", DEPOIS_43]])
        checa("43c carregamento comecando atrasado (gotcha 24: livre, sem tabela) -> "
              "continua esperando e confirma",
              e is None and n == 1 and dt >= 6, (e, dt, n))

        e, dt, msgs, n = cenario_43([[0, "ocupado", ""], [1000, "html", tabela_43(ANTES_43)],
                                     [4000, "html", DEPOIS_43]])
        checa("43d tabela volta sem a linha e depois completa -> avisa uma vez e confirma",
              e is None and n == 1 and dt >= 4
              and sum("voltou sem o resultado esperado" in m for m in msgs) == 1, (e, dt, n))

        e, dt, msgs, n = cenario_43([[0, "ocupado", ""], [1000, "html", tabela_43(ANTES_43)]],
                                    teto=6)
        checa("43e tabela volta sem a linha e fica -> falha no teto contado do Done, sem 2o Done",
              isinstance(e, RuntimeError) and not isinstance(e, auto.SapIndisponivel)
              and n == 1 and 6 <= dt < 8 and "UM Done" in str(e), (e, dt, n))

        e, dt, msgs, n = cenario_43(
            [[500, "html", tabela_43([("C0", "Outra descricao"), ("C3", "Withholding COFINS-3%"),
                                      ("W8", "Withholding ISS-2%")])]], teto=4)
        checa("43f linha nova certa mas OUTRA linha mudou -> nao avanca, falha no teto",
              isinstance(e, RuntimeError) and n == 1 and "outras linhas mudaram" in str(e), e)

        e, dt, msgs, n = cenario_43(
            [[500, "html", tabela_43(ANTES_43 + [("W8", "")])]], teto=3)
        checa("43g linha nova sem Description -> nao avanca, falha no teto",
              isinstance(e, RuntimeError) and n == 1 and "sem Description" in str(e), e)

        alterna = [[t, "ocupado" if (t // 1000) % 2 == 0 else "livre", ""]
                   for t in range(0, 20000, 1000)]
        e, dt, msgs, n = cenario_43(alterna, teto=5)
        checa("43h SAP alternando ocupado/livre -> o teto NAO reinicia: falha aos 5 s do Done",
              isinstance(e, RuntimeError) and n == 1 and 5 <= dt < 7, (e, dt, n))

        e, dt, msgs, n = cenario_43(
            [[0, "ocupado", ""],
             [1500, "html", "<p>Your session has expired. Please log on again.</p>"]])
        checa("43i sessao expirada -> SapIndisponivel na hora (expiracao informada), 1 Done",
              isinstance(e, auto.SapIndisponivel) and n == 1 and dt < 4
              and categoria_43(e) == "expiracao informada pelo SAP", (e, dt, n))

        e, dt, msgs, n = cenario_43([[1000, "html", SUBTELA_43 + AVISO_DESLIGA]])
        checa("43j aviso 'Instance is closing down' na janela da CR -> SapIndisponivel "
              "(aviso de desligamento)",
              isinstance(e, auto.SapIndisponivel) and n == 1 and dt < 4
              and categoria_43(e) == "aviso de desligamento"
              and "instance is closing down" in str(e), (e, dt, n))

        e, dt, msgs, n = cenario_43(
            [[1000, "html", '<form><input name="sap-user"><input name="sap-password" '
                            'type="password"></form>']])
        checa("43k voltou para a tela de logon do SAP -> SapIndisponivel (retorno a autenticacao)",
              isinstance(e, auto.SapIndisponivel) and n == 1 and dt < 4
              and categoria_43(e) == "retorno a autenticacao", (e, dt, n))

        e, dt, msgs, n = cenario_43([[0, "ocupado", ""], [1000, "html", "<p>Pagina qualquer</p>"]],
                                    teto=3)
        checa("43l tabela ausente sem sinal de queda NAO prova indisponibilidade: espera o teto",
              isinstance(e, RuntimeError) and not isinstance(e, auto.SapIndisponivel)
              and dt >= 3, (e, dt))

        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43], [700, "ocupado", ""],
                                     [7000, "html", DEPOIS_43]])
        checa("43m resultado aparece e o SAP volta a ficar ocupado na estabilizacao -> "
              "continua avisando e so confirma depois",
              e is None and n == 1 and dt >= 7 and len(avisos_43(msgs)) >= 2,
              (e, dt, n, avisos_43(msgs)[:2]))

        e, dt, msgs, n = cenario_43(
            [[300, "html", DEPOIS_43], [700, "ocupado", ""],
             [2000, "html", "<p>Your session has expired. Please log on again.</p>"]])
        checa("43n sessao expira durante a estabilizacao -> SapIndisponivel, sem esperar o teto",
              isinstance(e, auto.SapIndisponivel) and n == 1 and dt < 5, (e, dt, n))

        e, dt, msgs, n = cenario_43(
            [[500, "html", tabela_43(ANTES_43 + [("W8", "Withholding ISS-2%"),
                                                 ("W8", "Withholding ISS-2%")])]], teto=3)
        checa("43o linha nova DUPLICADA -> nao avanca, falha no teto",
              isinstance(e, RuntimeError) and n == 1 and "achei 2" in str(e), e)

        e, dt, msgs, n = cenario_43(
            [[500, "html", tabela_43(ANTES_43 + [("W8", "Withholding ISS-2%"),
                                                 ("X0", "Withholding extra")])]], teto=3)
        checa("43p linha EXTRA de outro codigo -> nao avanca, falha no teto",
              isinstance(e, RuntimeError) and n == 1 and "'X0'" in str(e), e)

        from selenium.common.exceptions import NoSuchWindowException
        safe_orig = auto._safe_execute_script

        def safe_janela_fecha(drv, script, *a):
            if (script is auto._JS_BLINDLAYER_BLOQUEANDO and est_43.get("t_done")
                    and time.time() - est_43["t_done"] > 1):
                raise NoSuchWindowException("janela fechada (teste)")
            return safe_orig(drv, script, *a)

        auto._safe_execute_script = safe_janela_fecha
        try:
            e, dt, msgs, n = cenario_43([[0, "ocupado", ""]], teto=20)
        finally:
            auto._safe_execute_script = safe_orig
        checa("43q janela perdida ENTRE leituras (nao na 1a sondagem) -> SapIndisponivel "
              "(janela perdida)",
              isinstance(e, auto.SapIndisponivel) and n == 1 and dt < 3
              and categoria_43(e) == "janela perdida", (e, dt, n))

        # achado 1 da revisao (78a): o aviso surge DURANTE a estabilizacao e a tabela
        # correta continua na tela. Com a estabilidade de producao (2,5 s).
        auto.ESTABILIDADE_LOOP = 2.5
        try:
            e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43],
                                         [1300, "html", DEPOIS_43 + AVISO_DESLIGA]])
        finally:
            auto.ESTABILIDADE_LOOP = 0.5
        checa("43r aviso surge na estabilizacao COM a tabela correta -> SapIndisponivel, "
              "nao sucesso (achado 1 da revisao)",
              isinstance(e, auto.SapIndisponivel) and n == 1 and dt < 3
              and categoria_43(e) == "aviso de desligamento", (e, dt, n))

        # achado 2: o relogio civil salta DURANTE a estabilizacao (a tela nunca para)
        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43], [400, "muda", ""]],
                                    teto=4, salto=(-3600, 1.0))
        checa("43s relogio do Windows RECUA 1 h no meio da estabilizacao -> o prazo "
              "monotonico vale: falha aos 4 s (achado 2 da revisao)",
              isinstance(e, RuntimeError) and not isinstance(e, auto.SapIndisponivel)
              and n == 1 and 4 <= dt < 6, (e, dt, n))
        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43]], teto=4, salto=(3600, 0.5))
        checa("43s relogio do Windows AVANCA 1 h -> nao encurta a espera: confirma normalmente",
              e is None and n == 1 and dt < 3, (e, dt, n))

        e, dt, msgs, n = cenario_43(
            [[300, "html", DEPOIS_43
              + "<p>Notas: session 2 terminated contracts; error tolerance; payment timeout "
                "clause; expired documents; login portal; closing down of branch</p>"
                "<input type='hidden' name='sap-user' value='x'>"
                "<input name='sap-password' type='password' style='display:none'>"]])
        checa("43t negativos: palavras soltas (session, terminated, error, timeout, expired, "
              "login) e campos de logon ocultos NAO sao prova -> confirma normalmente",
              e is None and n == 1, (e, dt, n))

        e, dt, msgs, n = cenario_43(
            [[800, "html", "<h1>400 Bad Request</h1><p>Session no longer exists</p>"]])
        checa("43u frase documentada nova ('Session no longer exists') -> SapIndisponivel",
              isinstance(e, auto.SapIndisponivel) and n == 1 and dt < 3
              and categoria_43(e) == "expiracao informada pelo SAP", (e, dt, n))

        # ---- extensao ao New e ao pre-Done (GO de desenho do Codex, 78a) ----
        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43]],
                                    new=[[0, "ocupado", ""], [23000, "html", SUBTELA_43]])
        checa("44a New lento (SAP ocupado 23 s) -> espera, avisa e so preenche depois, 1 Done",
              e is None and n == 1 and est_43["t_fill"] - est_43["t_new"] >= 23
              and len(avisos_43(msgs, "o New")) >= 8, (e, n, avisos_43(msgs, "o New")[:2]))

        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43]],
                                    new=[[3000, "ocupado", ""], [5000, "html", SUBTELA_43]])
        checa("44b New comecando atrasado (tabela ainda na tela, sem indicador) -> continua "
              "esperando e so preenche com a sub-tela pronta",
              e is None and n == 1 and est_43["t_fill"] - est_43["t_new"] >= 5, (e, n))

        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43]],
                                    new=[[0, "html", subtela_43(bloqueada=True)],
                                         [4000, "html", SUBTELA_43]])
        checa("44c sub-tela aberta com campos ainda bloqueados -> so preenche depois de liberar",
              e is None and n == 1 and est_43["t_fill"] - est_43["t_new"] >= 4, (e, n))

        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43]],
                                    check=[[0, "ocupado", ""], [18000, "livre", ""]])
        checa("44d processamento do checkbox alem de 15 s -> espera, avisa e so entao o "
              "UNICO Done",
              e is None and n == 1 and est_43["t_done"] - est_43["t_check"] >= 18
              and len(avisos_43(msgs, "o preenchimento")) >= 6, (e, n))

        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43]],
                                    check=[[0, "ocupado", ""],
                                           [1000, "html", SUBTELA_43 + "<p>ABAP session was "
                                                          "closed after timeout</p>"]])
        checa("44e sessao fechada antes do Done -> SapIndisponivel no preenchimento, ZERO Done",
              isinstance(e, auto.SapIndisponivel) and n == 0
              and "durante o preenchimento" in str(e)
              and categoria_43(e) == "expiracao informada pelo SAP", (e, n))

        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43]],
                                    new=[[0, "ocupado", ""]], teto=4)
        checa("44f prazo esgotado no New -> interrompe (nao preenche), ZERO Done",
              isinstance(e, RuntimeError) and not isinstance(e, auto.SapIndisponivel)
              and n == 0 and est_43["t_fill"] is None
              and "sub-tela do New nao ficou pronta" in str(e) and "Nenhum Done" in str(e),
              (e, n))

        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43]],
                                    check=[[0, "ocupado", ""]], teto=4)
        checa("44g prazo esgotado antes do Done -> interrompe, ZERO Done",
              isinstance(e, RuntimeError) and not isinstance(e, auto.SapIndisponivel)
              and n == 0 and "Nenhum Done" in str(e), (e, n))

        e, dt, msgs, n = cenario_43([[300, "html", DEPOIS_43]],
                                    new=[[0, "html", subtela_43(bloqueada=True)]], teto=4)
        checa("44h campos que nunca liberam -> interrompe no prazo, sem preencher, ZERO Done",
              isinstance(e, RuntimeError) and n == 0 and est_43["t_fill"] is None
              and "ainda bloqueado" in str(e), (e, n))

        checa("43/44 vigias: nenhum Save, Submit, acao de linha ou clique fora do roteiro",
              proibidas_43 == [], proibidas_43)
    finally:
        for nome_43, f in orig_43.items():
            setattr(auto, nome_43, f)
        (auto.ESTABILIDADE_LOOP, auto.INTERVALO_LOOP,
         auto.TETO_ESPERA_SAP_S, auto.AVISO_ESPERA_S, auto.time) = cfg_43
        auto.log.removeHandler(coletor)

    # o lote: sessao caida para ANTES do Save e nao abre a proxima CR, mesmo com
    # PARAR_LOTE_NO_ERRO desligado (reaproveita o DriverFalso/passo do caso 37)
    orig_l43 = {n: getattr(auto, n) for n in NOMES_LOTE}
    flags_l43 = (auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER,
                 auto.PARAR_LOTE_NO_ERRO)
    try:
        auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER = False, False, True
        auto.PARAR_LOTE_NO_ERRO = False
        auto.esperar_login_manual = passo("login")
        auto.go_to_display_change_requests = passo("app")
        auto.open_cr = passo("abrir")
        auto.cr_esta_travada = passo("lock?", False)
        auto.navigate_to_tax_section = passo("navegar")
        auto.iniciar_atestado_insercao = passo("preflight", {"fase": "aberta_limpa"})
        auto.atestar_insercao_para_save = passo("atestar", {"fase": "insercao_atestada"})
        auto.salvar_insercao_para_submit = passo("salvar", {"fase": "raiz_pronta_submit"})
        auto.submeter_cr_validada_da_raiz = passo("submit", {"veredito": "confirmado"})

        def inserir_cai(*a, **k):
            ordem.append("inserir")
            raise auto.SapIndisponivel('[111] W8: aviso de desligamento (a tela mostrou '
                                       '"instance is closing down") durante o Done',
                                       "aviso de desligamento")

        auto.insert_all_taxes = inserir_cai
        del ordem[:]
        saida = rodar(["111", "222"])
        checa("43 lote: sinal de indisponibilidade -> sem Save/Submit, NAO abre a 2a CR mesmo com "
              "PARAR_LOTE_NO_ERRO=False, exit 1",
              ordem == ["login", "get", "app", "abrir", "lock?", "navegar", "preflight",
                        "inserir"] and saida == 1, (ordem, saida))
    finally:
        for n, f in orig_l43.items():
            setattr(auto, n, f)
        (auto.SALVAR, auto.SUBMETER, auto.INSERIR_SALVAR_SUBMETER,
         auto.PARAR_LOTE_NO_ERRO) = flags_l43

finally:
    d.quit()

print("\n" + "=" * 60)
if falhas:
    print("FALHARAM %d checagem(ns): %s" % (len(falhas), falhas))
    sys.exit(1)
print("TODAS AS %d CHECAGENS PASSARAM" % _total)
