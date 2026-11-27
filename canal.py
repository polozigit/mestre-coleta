#!/usr/bin/env python3
"""Canal de suporte do Mestre das IAs: o Codex do aluno pede ajuda ao suporte Polozi e recebe a resposta.

Uso (dentro da pasta da empresa):
  canal.py ligar --aluno CODIGO        liga o canal neste computador (1 vez; o dono autoriza no chat antes)
  canal.py pedir --arquivo RELATORIO   envia o relatorio e espera a resposta (ate 10 min, no mesmo comando)
  canal.py buscar                      pega resposta que chegou depois (nao espera)

O que sobe: so o relatorio que o Codex escreveu, o sistema operacional, a versao do kit, o nome da empresa e a
etapa da instalacao (operacao/INSTALACAO.md) e as ultimas linhas de operacao/telemetria.jsonl. Chave, token, senha, e-mail, CPF e telefone viram [REMOVIDO] antes de sair
daqui. Nunca escreva no relatorio dado de cliente, hospede ou paciente da empresa.

So biblioteca padrao. Roda igual em Windows e Mac. A ultima linha da saida diz o resultado:
  RESPOSTA RECEBIDA | SEM RESPOSTA AINDA | PEDIDO ENVIADO | NADA PENDENTE | CANAL LIGADO | CANAL DESLIGADO | ERRO
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

VERSAO = "1.1"
ENDPOINT = "https://lgpagqxsxdsepxjmlnhc.supabase.co/functions/v1/mestre-canal"
RE_ALUNO = re.compile(r"[A-Za-z0-9_-]{22}")
MAX_RELATORIO = 8 * 1024       # bytes do relatorio escrito pelo Codex
MAX_TELEMETRIA = 3 * 1024      # bytes do rabo da telemetria
LINHAS_TELEMETRIA = 20
ESPERAS = (15, 30)             # depois disso, 60s por volta
ESPERA_LONGA = 60
ESPERA_MAX = 600               # teto da espera num comando so
ERROS_FINAIS = {"canal_desligado", "token_invalido"}

SEGREDO_RES = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sb_(?:secret|publishable)_[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sbp_[A-Za-z0-9]{20,}"),
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"EAA[A-Za-z0-9]{40,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)((?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|client[_-]?secret|password|senha|"
        r"service[_-]?role[_-]?key|bearer)[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]{8,})"
    ),
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+)([A-Za-z0-9._\-+/=]{8,})"),
]
# Dado pessoal: o canal fala do sistema, nunca dos clientes da empresa.
PII_RES = [
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<![\w.])\d{3}\.\d{3}\.\d{3}-\d{2}(?![\w.])"),
    re.compile(r"(?<![\w.])\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?![\w.])"),
    re.compile(r"(?<![\w.\-])(?:\+?55[\s-]?)?\(?\d{2}\)?[\s-]?9\d{4}[\s-]?\d{4}(?![\w.\-])"),
]


def redigir(texto: str) -> tuple[str, int]:
    total = 0
    for rx in SEGREDO_RES:
        if rx.groups >= 2:
            texto, n = rx.subn(lambda m: m.group(1) + "[REMOVIDO]", texto)
        else:
            texto, n = rx.subn("[REMOVIDO]", texto)
        total += n
    for rx in PII_RES:
        texto, n = rx.subn("[REMOVIDO]", texto)
        total += n
    return texto, total


def avisar(msg: str) -> None:
    print(msg, flush=True)


def cortar(texto: str, max_bytes: int, do_fim: bool = False) -> str:
    """Corta por bytes sem partir caractere. do_fim=True guarda o final (log: o erro esta no fim)."""
    dados = texto.encode("utf-8")
    if len(dados) <= max_bytes:
        return texto
    pedaco = dados[-max_bytes:] if do_fim else dados[:max_bytes]
    marca = "[... cortado ...]\n"
    limpo = pedaco.decode("utf-8", "ignore")
    return marca + limpo if do_fim else limpo + "\n" + marca


def eh_casa(p: Path) -> bool:
    return (p / "AGENTS.md").is_file() and ((p / "operacao").is_dir() or (p / ".codex").is_dir())


def achar_casa(inicio: Path) -> Path | None:
    p = inicio.resolve()
    for d in (p, *p.parents):
        if eh_casa(d):
            return d
    return None


def arquivo_token(casa: Path) -> Path:
    """Token mora em credenciais/ (fora do git e fechada para o agente). Se a pasta nao estiver ignorada
    pelo git nesta Casa, vai para a pasta do usuario: token nunca pode ser commitado."""
    cred = casa / "credenciais"
    alvo = cred / "canal-suporte.json"
    ignorado = False
    if shutil.which("git"):
        try:
            r = subprocess.run(["git", "-C", str(casa), "check-ignore", "-q", str(alvo)],
                               capture_output=True, timeout=10)
            ignorado = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ignorado = False
    if ignorado:
        return alvo
    marca = hashlib.sha256(os.path.normcase(str(casa)).encode("utf-8")).hexdigest()[:12]
    return Path.home() / ".polozi" / f"canal-suporte-{marca}.json"


def ler_token(casa: Path) -> str | None:
    for arq in (casa / "credenciais" / "canal-suporte.json", arquivo_token(casa)):
        try:
            return json.loads(arq.read_text(encoding="utf-8"))["token"]
        except (OSError, ValueError, KeyError):
            continue
    return None


def gravar_token(casa: Path, token: str) -> Path:
    arq = arquivo_token(casa)
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps({"token": token, "ligado_em": datetime.now().isoformat(timespec="seconds")}),
                   encoding="utf-8")
    try:
        os.chmod(arq, 0o600)
    except OSError:
        pass
    return arq


def http_json(corpo: dict, timeout: int = 30) -> dict:
    dados = json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(ENDPOINT, data=dados, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except ValueError:
            return {"ok": False, "erro": f"http {e.code}"}
    except urllib.error.URLError as e:
        if "CERTIFICATE" in str(e).upper() and shutil.which("curl"):
            r = subprocess.run(
                ["curl", "-sS", "-X", "POST", "-H", "Content-Type: application/json", "--data-binary", "@-", ENDPOINT],
                input=dados, capture_output=True, timeout=timeout,
            )
            return json.loads(r.stdout.decode("utf-8") or "{}")
        raise


def chamar(corpo: dict) -> dict:
    """Uma chamada ao canal. Falha de rede vira {'ok': False, 'erro': 'rede: ...'} (quem decide repetir e o chamador)."""
    try:
        r = http_json(corpo)
    except Exception as e:  # rede, proxy, sandbox
        return {"ok": False, "erro": f"rede: {e.__class__.__name__}: {str(e)[:120]}"}
    return r if isinstance(r, dict) else {"ok": False, "erro": "resposta invalida"}


RE_ETAPA = re.compile(r"^etapa_atual:\s*([A-Za-z0-9_.-]{1,40})\s*$", re.M)
RE_EMPRESA = re.compile(r"trabalha para ([^.{}\n]{2,80})\.")


def meta_da_casa(casa: Path) -> dict:
    meta = {"so": f"{platform.system()} {platform.release()}"[:40], "versao_script": VERSAO}
    try:
        m = RE_ETAPA.search((casa / "operacao" / "INSTALACAO.md").read_text(encoding="utf-8", errors="replace")[:4000])
        if m:
            meta["etapa"] = m.group(1)
    except OSError:
        pass
    try:
        m = RE_EMPRESA.search((casa / "AGENTS.md").read_text(encoding="utf-8", errors="replace")[:4000])
        if m:
            meta["empresa"] = m.group(1).strip()[:80]
    except OSError:
        pass
    try:
        kit = json.loads((casa / "operacao" / "KIT.json").read_text(encoding="utf-8"))
        sha = str(kit.get("origem_git_sha") or "")
        if sha:
            meta["kit_sha"] = sha[:64]
    except (OSError, ValueError):
        pass
    return meta


def rabo_telemetria(casa: Path) -> str:
    try:
        linhas = (casa / "operacao" / "telemetria.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return cortar("\n".join(linhas[-LINHAS_TELEMETRIA:]), MAX_TELEMETRIA, do_fim=True)


def montar_pedido(casa: Path, relatorio: str) -> tuple[str, int]:
    """Relatorio do Codex + contexto automatico, ja sem segredo e sem dado pessoal."""
    partes = [cortar(relatorio.strip(), MAX_RELATORIO)]
    meta = meta_da_casa(casa)
    partes.append(f"\n---\nSistema: {meta['so']} | kit: {meta.get('kit_sha', 'sem KIT.json')} | canal.py {VERSAO}")
    tele = rabo_telemetria(casa)
    if tele:
        partes.append("Ultimas linhas de operacao/telemetria.jsonl:\n" + tele)
    return redigir("\n".join(partes))


def mostrar_respostas(casa: Path, respostas: list) -> None:
    for r in respostas:
        corpo = str(r.get("corpo", ""))
        avisar("===== RESPOSTA DO SUPORTE POLOZI =====")
        avisar(f"(conversa {r.get('conversa_id', '')})")
        avisar(corpo)
        avisar("===== FIM DA RESPOSTA =====")
        pasta = casa / "operacao" / "suporte"
        if (casa / "operacao").is_dir():
            try:
                pasta.mkdir(exist_ok=True)
                nome = datetime.now().strftime("%Y-%m-%d-%H%M%S") + "-resposta.md"
                (pasta / nome).write_text(corpo + "\n", encoding="utf-8")
            except OSError:
                pass


def fim_por_erro(erro: str) -> int:
    if erro == "canal_desligado":
        avisar("O canal de suporte esta desligado. Nao tente de novo: avise o dono e siga pelo WhatsApp do curso.")
        avisar("CANAL DESLIGADO")
        return 3
    if erro == "token_invalido":
        avisar("O codigo deste computador nao vale mais. O dono precisa pedir ao Polozi para religar o canal.")
        avisar("CANAL DESLIGADO")
        return 3
    avisar(f"Nao deu certo: {erro}")
    avisar("ERRO")
    return 1


def esperar_resposta(casa: Path, token: str, espera_max: int) -> int:
    """Consulta com espera crescente (15s, 30s, depois 60s) ate o teto. Nunca fica em laco sem fim."""
    gasto, volta = 0, 0
    while True:
        r = chamar({"acao": "buscar", "token": token})
        if r.get("ok"):
            respostas = r.get("respostas") or []
            if respostas:
                mostrar_respostas(casa, respostas)
                avisar("RESPOSTA RECEBIDA")
                return 0
            if not r.get("aguardando"):
                avisar("NADA PENDENTE")
                return 0
        elif r.get("erro") in ERROS_FINAIS:
            return fim_por_erro(str(r.get("erro")))
        espera = ESPERAS[volta] if volta < len(ESPERAS) else ESPERA_LONGA
        if gasto + espera > espera_max:
            avisar("O suporte ainda nao respondeu. A resposta fica guardada: rode `canal.py buscar` mais tarde,")
            avisar("no maximo 3 vezes, e enquanto isso siga com o que nao depende dela ou avise o dono.")
            avisar("SEM RESPOSTA AINDA")
            return 2
        avisar(f"  Esperando o suporte ({gasto}s de {espera_max}s)...")
        time.sleep(espera)
        gasto += espera
        volta += 1


def cmd_ligar(casa: Path, args) -> int:
    aluno = (args.aluno or "").strip()
    if not RE_ALUNO.fullmatch(aluno):
        avisar("Codigo de aluno invalido: sao 22 caracteres, como veio na mensagem do Polozi.")
        avisar("ERRO")
        return 1
    r = chamar({"acao": "ligar", "aluno": aluno})
    if r.get("ok") and r.get("token"):
        arq = gravar_token(casa, r["token"])
        avisar(f"Canal ligado neste computador (codigo guardado em {arq}).")
        avisar("CANAL LIGADO")
        return 0
    if r.get("erro") == "ja_ligado":
        if ler_token(casa):
            avisar("O canal ja estava ligado neste computador.")
            avisar("CANAL LIGADO")
            return 0
        avisar("O canal ja foi ligado em outro computador ou pasta. O dono precisa pedir ao Polozi para religar.")
        avisar("ERRO")
        return 1
    return fim_por_erro(str(r.get("erro")))


def cmd_pedir(casa: Path, args) -> int:
    token = ler_token(casa)
    if not token:
        avisar("O canal nao esta ligado neste computador. Rode antes: canal.py ligar --aluno CODIGO")
        avisar("ERRO")
        return 1
    try:
        relatorio = Path(args.arquivo).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        avisar(f"Nao consegui ler o relatorio: {e}")
        avisar("ERRO")
        return 1
    if not relatorio.strip():
        avisar("O relatorio esta vazio.")
        avisar("ERRO")
        return 1
    corpo, removidos = montar_pedido(casa, relatorio)
    pedido = {"acao": "enviar", "token": token, "corpo": corpo, "meta": meta_da_casa(casa)}
    if args.conversa:
        pedido["conversa_id"] = args.conversa
    r = chamar(pedido)
    if not r.get("ok"):
        return fim_por_erro(str(r.get("erro")))
    avisar(f"Pedido enviado ao suporte (conversa {r.get('conversa_id')}; {removidos} trecho(s) sensivel(is) removido(s)).")
    if args.sem_esperar:
        avisar("PEDIDO ENVIADO")
        return 0
    return esperar_resposta(casa, token, args.espera_max)


def cmd_buscar(casa: Path, args) -> int:
    token = ler_token(casa)
    if not token:
        avisar("O canal nao esta ligado neste computador.")
        avisar("ERRO")
        return 1
    return esperar_resposta(casa, token, args.espera_max if args.esperar else 0)


def main(argv: list[str] | None = None) -> int:
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Canal de suporte do Mestre das IAs")
    ap.add_argument("--casa", help="pasta da empresa (padrao: a pasta atual)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ligar")
    p.add_argument("--aluno", required=True)
    p = sub.add_parser("pedir")
    p.add_argument("--arquivo", required=True)
    p.add_argument("--conversa")
    p.add_argument("--sem-esperar", action="store_true")
    p.add_argument("--espera-max", type=int, default=ESPERA_MAX)
    p = sub.add_parser("buscar")
    p.add_argument("--esperar", action="store_true")
    p.add_argument("--espera-max", type=int, default=ESPERA_MAX)
    args = ap.parse_args(argv)
    if hasattr(args, "espera_max"):
        args.espera_max = max(0, min(args.espera_max, ESPERA_MAX))

    casa = achar_casa(Path(args.casa) if args.casa else Path.cwd())
    if not casa:
        avisar("Rode dentro da pasta da empresa (a que tem o AGENTS.md).")
        avisar("ERRO")
        return 1
    return {"ligar": cmd_ligar, "pedir": cmd_pedir, "buscar": cmd_buscar}[args.cmd](casa, args)


if __name__ == "__main__":
    sys.exit(main())
