"""
LIMPA uma CR de TESTE: apaga as linhas de Withholding Tax e SALVA, deixando a
tabela vazia de novo - pra CR servir a mais um teste de insercao (otimizacao
de tempo, fluxo final). Decisao do operador em 28/09: nao pedir mais CRs ao
negocio e reaproveitar as 5 de teste.

APAGA DADO, entao e' fail-closed:
  - so roda com AMBIENTE = "pre_producao";
  - so aceita as CRs de CRS_DE_TESTE (qualquer outra encerra ANTES de abrir o
    navegador);
  - so apaga codigos de TAX_CODES: uma linha fora da lista (ou repetida) aborta
    ANTES do 1o delete - seria dado de negocio, nao nosso;
  - confirmacao digitada: LIMPAR;
  - as travas de navegacao do main() (X401, 1 linha em cada tabela) e as do
    deletar_linha_imposto (1 linha do codigo antes, delta exato depois).
Nao vai no pacote da usuaria.

O Delete de linha NUNCA foi clicado no SAP ate 28/09: a 1a execucao e' tambem
o teste dele. Se ele se comportar diferente do esperado (pedir confirmacao,
marcar a linha em vez de remover), o delta nao fecha, o script para no 1o
codigo e a CR fica travada por algumas horas - nada e' salvo pela metade.

Uso (na pasta do projeto):  python codigo\\limpar_cr_teste.py
"""

import os
import sys
from datetime import datetime

import sap_ishift_tax_automation as auto

CRS_DE_TESTE = {"90000001", "90000003", "90000002", "90000004", "90000005"}


def validar_crs(crs, teve_invalido=False):
    """Levanta SystemExit se a lista teve entrada invalida (ex.: numero
    digitado errado, que o get_cr_list descarta) ou se alguma CR nao e' de teste."""
    if teve_invalido:
        raise SystemExit("Recusado: a lista tinha entrada(s) invalida(s). Confira e cole de novo.")
    fora = [c for c in crs if c not in CRS_DE_TESTE]
    if fora:
        raise SystemExit(f"Recusado: {fora} nao e' CR de teste. Permitidas: {sorted(CRS_DE_TESTE)}.")


def codigos_a_apagar(tipos):
    """Types lidos da tabela -> codigos a apagar. Levanta se houver linha que
    nao e' dos 64 ou codigo repetido: nesse caso nao se apaga NADA."""
    alheios = sorted({t for t in tipos if t not in auto.TAX_CODES})
    if alheios:
        raise RuntimeError(f"a tabela tem linha(s) fora dos 64 codigos: {alheios}. Nada apagado.")
    repetidos = sorted({t for t in tipos if tipos.count(t) > 1})
    if repetidos:
        raise RuntimeError(f"codigo(s) repetido(s) na tabela: {repetidos}. Nada apagado.")
    return list(tipos)


def mensagem_de_falha(fase):
    """O que dizer ao operador conforme ONDE a limpeza parou (NO-GO do Codex:
    antes afirmava 'sem Save' mesmo se a falha viesse depois do clique)."""
    if fase == "salvando":
        return ("falhou DURANTE ou DEPOIS do Save: ele PODE ter gravado. NAO repita esta CR - "
                "abra no SAP e confira a tabela antes de qualquer coisa.")
    if fase in ("apagando", "voltando"):
        return ("falhou ANTES do Save, com linhas apagadas so' nesta sessao: nada foi gravado "
                "(o que nao e' salvo se perde, visto em 14/09), mas a CR ficou TRAVADA - de "
                "algumas horas a ~1 dia.")
    if fase == "navegando":
        return ("falhou antes de apagar qualquer linha: nada foi gravado, mas a CR pode ter "
                "ficado TRAVADA - de algumas horas a ~1 dia.")
    return "falhou antes de entrar na CR: nada foi alterado."


def limpar_cr(driver, cr, estado):
    """Abre a CR, apaga todas as linhas (so as nossas) e salva. Devolve o
    veredito do Save ('confirmado' / 'ressalva'). `estado['fase']` diz onde
    parou, se levantar."""
    estado["fase"] = "abrindo"
    driver.get(auto.ISHIFT_URL)
    auto.go_to_display_change_requests(driver)
    auto.open_cr(driver, cr)
    if auto.cr_esta_travada(driver):
        raise RuntimeError(f"CR {cr} travada ao abrir - tentar mais tarde.")
    estado["fase"] = "navegando"
    auto.navigate_to_tax_section(driver)
    codes = codigos_a_apagar(auto.ler_tabela_impostos_integral(driver))
    auto.log.info(f"[{cr}] apagando {len(codes)} linha(s): {codes}")
    estado["fase"] = "apagando"
    if codes:
        restantes = auto.limpar_impostos(driver, codes, cr_number=cr, forcar=True)
        if restantes:
            raise RuntimeError(f"[{cr}] sobraram linhas depois do delete: {restantes}")
    # Save mesmo com a tabela ja vazia: navegar abre edicao (gotcha 19) e so
    # Save/Undo soltam o lock (gotcha 7). A "tabela vazia no servidor" se
    # confirma na rodada seguinte: o preflight da insercao exige tabela vazia.
    estado["fase"] = "voltando"
    auto.fechar_subtelas_para_raiz(driver, cr)
    estado["fase"] = "salvando"
    # a fase fica "salvando" ate a proxima CR: o mais conservador (Codex, 28/09)
    return auto.salvar_cr_da_raiz(driver, cr).get("veredito")


def main():
    auto._ativar_log_em_arquivo()
    if auto.AMBIENTE != "pre_producao":
        sys.exit("Recusado: limpar CR so em pre_producao (config.py).")
    auto.STEP_CONFIRM = False

    print("\n" + "=" * 64)
    print("  LIMPAR CR DE TESTE: apaga as linhas de Withholding Tax e SALVA")
    print("  (a tabela fica vazia, pronta pra um novo teste de insercao).")
    print("=" * 64)
    crs, teve_invalido = auto.get_cr_list()
    if not crs:
        sys.exit("Nenhuma CR informada. Nada foi feito.")
    validar_crs(crs, teve_invalido)
    print(f"\n  Vou LIMPAR e SALVAR: {crs}")
    try:
        ok = auto._pedir_confirmacao("LIMPAR")
    except EOFError:
        ok = False
    if not ok:
        sys.exit("Confirmacao nao recebida. Nada foi feito.")

    driver = auto.criar_driver_edge()
    saida = 0
    estado = {"fase": "login"}
    try:
        driver.maximize_window()
        driver.get(auto.ISHIFT_URL)
        auto.esperar_login_manual(driver)
        for cr in crs:
            estado["cr"] = cr
            veredito = limpar_cr(driver, cr, estado)
            auto.log.info(f"[{cr}] limpa. Save: {veredito}")
            if veredito != "confirmado":
                auto.log.warning(f"[{cr}] Save com {veredito!r}: parando. NAO repita esta CR "
                                 f"sem conferir no SAP.")
                saida = 3
                break
    except Exception:
        auto.log.exception(f"Limpeza interrompida na CR {estado.get('cr', '-')} "
                           f"(fase: {estado['fase']}): {mensagem_de_falha(estado['fase'])}")
        try:
            driver.save_screenshot(os.path.join(
                auto.PRINTS_DIR, f"limpar_falha_{datetime.now():%Y%m%d_%H%M%S}.png"))
        except Exception:
            pass
        saida = 3 if estado["fase"] == "salvando" else 1
    finally:
        try:
            input("\nPressione ENTER para fechar o navegador...")
        except EOFError:
            auto.esperar_inspecao()
        driver.quit()
    auto.log.info(f"Log completo salvo em: {auto.LOG_FILE}")
    sys.exit(saida)


if __name__ == "__main__":
    main()
