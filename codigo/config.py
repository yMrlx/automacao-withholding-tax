"""
Configuracao de ambiente - separada do codigo principal de proposito.

Troque AMBIENTE pra "producao" so depois de validar o fluxo inteiro contra a
CR de teste em pre-producao. Ter a URL aqui, fora do script, reduz o risco de
mudar o ambiente sem querer no meio de uma alteracao de codigo - e deixa
explicito, em UM lugar so, contra qual sistema o script vai rodar.

Usado por todos os scripts em codigo/ (script principal e ferramentas de
diagnostico), pra nao ter a URL duplicada em varios arquivos.
"""

AMBIENTE = "pre_producao"  # "pre_producao" ou "producao"

URLS = {
    "pre_producao": (
        "https://sap-preproducao.exemplo.com:44350/sap/bc/ui5_ui5/ui2/ushell/"
        "shells/abap/FioriLaunchpad.html?saml2=disabled&spnego=disabled"
    ),
    # Link "Home" mandado pelo negocio em 05/10 (sem o "#Shell-home": o
    # launchpad ja abre na home). Diferente da pre-producao, nao tem
    # saml2=disabled, entao o login deve ser SSO, sem a tela classica onde se
    # escolhe "EN - English". Por isso o sap-language=EN (parametro padrao do
    # SAP): os seletores por texto do script so funcionam com a tela em ingles.
    "producao": (
        "https://sap-producao.exemplo.com:44350/sap/bc/ui5_ui5/ui2/ushell/"
        "shells/abap/FioriLaunchpad.html?sap-client=100&sap-language=EN"
    ),
}

if AMBIENTE not in URLS:
    raise ValueError(f"AMBIENTE={AMBIENTE!r} invalido. Use 'pre_producao' ou 'producao'.")

ISHIFT_URL = URLS[AMBIENTE]
if ISHIFT_URL is None:
    raise ValueError(
        f"URL nao configurada pro ambiente {AMBIENTE!r} em config.py. "
        "Preencha URLS['producao'] antes de rodar contra producao."
    )
