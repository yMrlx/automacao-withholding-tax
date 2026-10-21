"""
Abre UMA CR pelo mesmo caminho e na mesma tela da automacao e PARA, pra um
teste MANUAL - ex.: clicar Submit com a propria mao e ver o que o SAP faz.

Motivo (29/09): o Edge normal entra por SSO com o usuario corporativo, e uma
guia anonima cai no login da Microsoft. A automacao usa a URL do config.py,
com saml2=disabled&spnego=disabled, que mostra o login SAP de sempre.

READ-ONLY: este script nao clica em Edit, Details, Save nem Submit. Quem
clica e' voce, na janela do Edge. Nao vai no pacote da usuaria.

Uso (na pasta do projeto):  python codigo\\abrir_cr_teste_manual.py
"""

import sys

import sap_ishift_tax_automation as auto


def main():
    auto.STEP_CONFIRM = False
    crs, teve_invalido = auto.get_cr_list()
    if teve_invalido or len(crs) != 1:
        sys.exit("Informe exatamente UMA CR. Nada foi aberto.")
    cr = crs[0]

    driver = auto.criar_driver_edge()
    try:
        driver.maximize_window()
        driver.get(auto.ISHIFT_URL)
        auto.esperar_login_manual(driver)
        auto.go_to_display_change_requests(driver)
        auto.open_cr(driver, cr)
        aviso = "  ATENCAO: ela aparece TRAVADA ('locked by')." if auto.cr_esta_travada(driver) else ""
        print(f"\nPronto: a CR {cr} esta aberta na janela do Edge.{aviso}")
        print("Faca o teste manual la. Este programa NAO clica em nada.")
        input("Quando terminar, pressione ENTER aqui para fechar o navegador...")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
