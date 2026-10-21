"""
Gera entrega/Automacao-Withholding-Tax/ e o .zip pra usuaria. Chamado pelo
"Criar pacote.bat".

Por que um script e nao so copiar: as flags de teste (SALVAR, tempos) vivem
ARMADAS no working tree durante as rodadas de validacao, e a copia antiga
mandava pra usuaria o arquivo do jeito que estivesse. Aqui:
  - so empacota o que esta COMMITADO: arquivo do pacote com mudanca local
    (staged ou nao) recusa o pacote inteiro;
  - liga o fluxo unico (INSERIR_SALVAR_SUBMETER = True) SO na copia - no
    repositorio o default continua "teste", que nao grava nada;
  - com o argumento "producao", troca o AMBIENTE SO na copia.
Cada troca exige achar a linha exata uma unica vez; senao, nada e' gerado.

Uso: python codigo\\gerar_pacote.py [producao]
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NOME = "Automacao-Withholding-Tax"
ARQUIVOS = ["Rodar automacao.bat", "README.md", "requirements.txt",
            "codigo/config.py", "codigo/sap_ishift_tax_automation.py"]


def trocar_linha(texto, linha, nova, arquivo):
    """Troca a linha EXATA `linha` (inteira, sem espacos extras) por `nova`.
    Levanta se ela nao aparecer exatamente uma vez."""
    padrao = re.compile(r"^" + re.escape(linha) + r"(?=\r?$)", re.M)
    novo, n = padrao.subn(nova, texto)
    if n != 1:
        raise SystemExit(f"[ERRO] {arquivo}: esperava a linha '{linha}' exatamente 1 vez, "
                         f"achei {n}. Pacote NAO gerado.")
    return novo


def exigir_linha(texto, linha, arquivo):
    trocar_linha(texto, linha, linha, arquivo)


def ajustar_script(texto):
    arq = "sap_ishift_tax_automation.py"
    # os outros dois modos tem que estar desligados no commit: os tres sao
    # mutuamente exclusivos e o main() recusaria a config
    exigir_linha(texto, "SALVAR = False", arq)
    exigir_linha(texto, "SUBMETER = False", arq)
    return trocar_linha(texto, "INSERIR_SALVAR_SUBMETER = False",
                        "INSERIR_SALVAR_SUBMETER = True", arq)


def ajustar_config(texto, ambiente):
    if ambiente == "pre_producao":
        exigir_linha(texto, 'AMBIENTE = "pre_producao"  # "pre_producao" ou "producao"',
                     "config.py")
        return texto
    return trocar_linha(texto, 'AMBIENTE = "pre_producao"  # "pre_producao" ou "producao"',
                        'AMBIENTE = "producao"  # "pre_producao" ou "producao"', "config.py")


def ambiente_do_argumento(argv):
    arg = (argv[1] if len(argv) > 1 else "").strip().lower()
    if arg in ("", "pre_producao"):
        return "pre_producao"
    if arg == "producao":
        return "producao"
    raise SystemExit(f"[ERRO] ambiente {arg!r} invalido: use 'producao' ou nada. "
                     f"Pacote NAO gerado.")


def arquivos_com_mudanca_local():
    r = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", *ARQUIVOS],
                       cwd=RAIZ, capture_output=True, text=True, check=True)
    return r.stdout.split()


def main(argv):
    ambiente = ambiente_do_argumento(argv)
    sujos = arquivos_com_mudanca_local()
    if sujos:
        raise SystemExit(
            f"[ERRO] mudanca NAO commitada em: {sujos}. O pacote sai so do que esta "
            f"commitado (ex.: flags de teste armadas). Desfaca ou commite e rode de novo. "
            f"Pacote NAO gerado.")

    entrega = RAIZ / "entrega"
    stage = entrega / NOME
    # ajusta tudo em memoria ANTES de apagar o pacote anterior
    script = ajustar_script((RAIZ / ARQUIVOS[4]).read_text(encoding="utf-8"))
    config = ajustar_config((RAIZ / ARQUIVOS[3]).read_text(encoding="utf-8"), ambiente)

    if entrega.exists():
        shutil.rmtree(entrega)
    (stage / "codigo").mkdir(parents=True)
    for rel in ARQUIVOS[:3]:
        shutil.copy2(RAIZ / rel, stage / rel)
    # .bat com linha em LF quebra no cmd (visto em 23/09): forca CRLF na copia
    bat = stage / ARQUIVOS[0]
    bat.write_bytes(bat.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    # newline="" grava o texto como lido, sem converter fim de linha
    (stage / ARQUIVOS[3]).write_text(config, encoding="utf-8", newline="")
    (stage / ARQUIVOS[4]).write_text(script, encoding="utf-8", newline="")
    for pasta in ("logs", "prints"):
        (stage / pasta).mkdir()
        (stage / pasta / ".gitkeep").touch()
    zip_path = shutil.make_archive(str(entrega / NOME), "zip", root_dir=entrega, base_dir=NOME)

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=RAIZ,
                            capture_output=True, text=True, check=True).stdout.strip()
    print("=" * 60)
    print(f"  Pacote pronto (commit {commit}):")
    print(f"    {zip_path}   <- envie este arquivo")
    print(f"    {stage}")
    print(f"  Ambiente: {ambiente.upper()}")
    print("  Modo: inserir, conferir, SALVAR e ENVIAR para aprovacao (fluxo unico)")
    print("=" * 60)


if __name__ == "__main__":
    main(sys.argv)
