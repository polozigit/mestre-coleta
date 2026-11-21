#!/usr/bin/env python3
"""Coleta do Mestre das IAs: junta as conversas do Codex/Claude e a pasta da empresa num zip e envia.

Para que serve: o Polozi estuda onde cada aluno travou, para melhorar o curso e o kit.

O que entra no zip:
  - conversas do Codex (~/.codex/sessions e archived_sessions), config.toml e AGENTS.md globais, logs
  - conversas do Claude Code (~/.claude/projects), se existirem
  - a pasta da empresa (Casa) com o historico do git
O que NAO entra: pasta credenciais/, arquivos .env, chaves (.pem/.key), node_modules, ambientes virtuais,
arquivos acima de 25 MB. Chaves de API e tokens dentro dos textos sao trocados por [REMOVIDO].

Uso: rode dentro da pasta da empresa.
  python3 coletar.py              gera o zip e envia
  python3 coletar.py --sem-enviar so gera o zip
  python3 coletar.py --pasta CAMINHO  aponta a pasta da empresa
So biblioteca padrao. Mac e Windows.
"""
from __future__ import annotations

import argparse
import datetime as dt
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
import zipfile
from collections import Counter
from pathlib import Path

VERSAO = "1.0"
ENDPOINT = "https://lgpagqxsxdsepxjmlnhc.supabase.co/functions/v1/mestre-coleta-upload"
MAX_ARQUIVO = 25 * 1024 * 1024
MAX_REDIGIR = 80 * 1024 * 1024
MAX_ZIP = 480 * 1024 * 1024

DIRS_FORA = {
    "node_modules", ".venv", "venv", "env", "__pycache__", ".next", "dist", "build", ".turbo",
    ".cache", "credenciais", ".pytest_cache", ".mypy_cache", ".DS_Store", ".Trash",
}
ARQ_FORA_RE = re.compile(r"(^\.env($|\.)|\.pem$|\.key$|\.p12$|\.pfx$|\.keystore$|^id_rsa|^id_ed25519|^coleta-mestre-.*\.zip$)", re.I)
TEXTO_EXT = {
    ".jsonl", ".json", ".toml", ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".sql", ".yaml", ".yml", ".sh", ".ps1", ".bat", ".html", ".css", ".csv", ".log", ".ini", ".cfg", ".env",
}

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


def redigir(texto: str) -> tuple[str, int]:
    total = 0
    for rx in SEGREDO_RES:
        if rx.groups >= 2:
            texto, n = rx.subn(lambda m: m.group(1) + "[REMOVIDO]", texto)
        else:
            texto, n = rx.subn("[REMOVIDO]", texto)
        total += n
    return texto, total


def avisar(msg: str) -> None:
    print(msg, flush=True)


def eh_casa(p: Path) -> bool:
    return (p / "AGENTS.md").is_file() and ((p / "operacao").is_dir() or (p / ".codex").is_dir())


def achar_casa(argumento: str | None, cwds_sessoes: Counter) -> Path | None:
    if argumento:
        p = Path(argumento).expanduser().resolve()
        return p if p.is_dir() else None
    atual = Path.cwd().resolve()
    for p in [atual, *atual.parents]:
        if eh_casa(p):
            return p
    for cwd, _ in cwds_sessoes.most_common():
        p = Path(cwd)
        try:
            if p.is_dir() and eh_casa(p):
                return p
        except OSError:
            continue
    return atual


def ler_identidade(casa: Path | None) -> dict:
    ident = {"email_dono": "", "nome_dono": "", "nome_empresa": ""}
    if not casa:
        return ident
    inst = casa / "operacao" / "INSTALACAO.md"
    try:
        m = re.search(r"email_dono:\s*([^\s{}]+@[^\s{}]+)", inst.read_text(encoding="utf-8", errors="replace"))
        if m:
            ident["email_dono"] = m.group(1).strip()
    except OSError:
        pass
    try:
        ag = (casa / "AGENTS.md").read_text(encoding="utf-8", errors="replace")[:4000]
        m = re.search(r"trabalha para ([^.{}\n]+)\.", ag)
        if m:
            ident["nome_empresa"] = m.group(1).strip()
        m = re.search(r"Dono:\s*([^,{}\n]+)", ag)
        if m:
            ident["nome_dono"] = m.group(1).strip()
    except OSError:
        pass
    return ident


def candidatos_home(var: str, nome: str) -> list[Path]:
    saida = []
    if os.environ.get(var):
        saida.append(Path(os.environ[var]).expanduser())
    saida.append(Path.home() / nome)
    if os.environ.get("USERPROFILE"):
        saida.append(Path(os.environ["USERPROFILE"]) / nome)
    vistos, unicos = set(), []
    for p in saida:
        k = str(p.resolve()) if p.exists() else str(p)
        if k not in vistos:
            vistos.add(k)
            unicos.append(p)
    return [p for p in unicos if p.is_dir()]


def busca_curta_rollouts(limite_s: float = 20.0) -> list[Path]:
    """Ultimo recurso: procura pasta com rollout-*.jsonl perto da pasta do usuario."""
    inicio, achados = time.time(), set()
    pular = {"Library", "AppData", "node_modules", ".Trash", "Pictures", "Music", "Movies", "Videos"}
    base = Path.home()
    for raiz, dirs, arqs in os.walk(base):
        if time.time() - inicio > limite_s:
            break
        prof = len(Path(raiz).relative_to(base).parts)
        dirs[:] = [d for d in dirs if d not in pular and prof < 5]
        if any(a.startswith("rollout-") and a.endswith(".jsonl") for a in arqs):
            p = Path(raiz)
            while p != base and p.name != "sessions":
                p = p.parent
            achados.add(p.parent if p.name == "sessions" else Path(raiz))
    return sorted(achados)


def cwd_da_sessao(arq: Path) -> str | None:
    try:
        with arq.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(5):
                linha = f.readline()
                if not linha:
                    break
                m = re.search(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"', linha)
                if m:
                    return json.loads('"' + m.group(1) + '"')
    except (OSError, ValueError):
        pass
    return None


class Coletor:
    def __init__(self, zip_path: Path):
        self.zip_path = zip_path
        self.zf = zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6)
        self.pulados: list[dict] = []
        self.contagem: Counter = Counter()
        self.removidos = 0

    def add(self, origem: Path, destino: str, categoria: str, redigir_texto: bool = True) -> None:
        try:
            tam = origem.stat().st_size
        except OSError as e:
            self.pulados.append({"arquivo": destino, "motivo": f"erro: {e.__class__.__name__}"})
            return
        if tam > MAX_ARQUIVO and categoria != "conversa":
            self.pulados.append({"arquivo": destino, "motivo": f"grande ({tam // 1048576} MB)"})
            return
        if tam > MAX_REDIGIR:
            self.pulados.append({"arquivo": destino, "motivo": f"grande ({tam // 1048576} MB)"})
            return
        try:
            if redigir_texto and (origem.suffix.lower() in TEXTO_EXT or origem.name in {"AGENTS.md", "CLAUDE.md"}):
                bruto = origem.read_bytes()
                try:
                    texto = bruto.decode("utf-8")
                    texto, n = redigir(texto)
                    self.removidos += n
                    self.zf.writestr(destino, texto)
                except UnicodeDecodeError:
                    self.zf.write(origem, destino)
            else:
                self.zf.write(origem, destino)
            self.contagem[categoria] += 1
        except OSError as e:
            self.pulados.append({"arquivo": destino, "motivo": f"erro: {e.__class__.__name__}"})

    def add_arvore(self, raiz: Path, prefixo: str, categoria: str, fora_git: bool = True) -> None:
        for dirpath, dirs, arqs in os.walk(raiz):
            dirs[:] = [d for d in dirs if d not in DIRS_FORA]
            rel_dir = Path(dirpath).relative_to(raiz)
            dentro_git = rel_dir.parts[:1] == (".git",)
            for a in arqs:
                if ARQ_FORA_RE.search(a):
                    self.pulados.append({"arquivo": f"{prefixo}/{(rel_dir / a).as_posix()}", "motivo": "sensivel"})
                    continue
                origem = Path(dirpath) / a
                if origem.is_symlink():
                    continue
                self.add(origem, f"{prefixo}/{(rel_dir / a).as_posix()}", categoria, redigir_texto=not dentro_git)

    def fechar(self, manifesto: dict) -> None:
        manifesto["contagem"] = dict(self.contagem)
        manifesto["segredos_removidos"] = self.removidos
        manifesto["pulados"] = self.pulados[:500]
        manifesto["pulados_total"] = len(self.pulados)
        self.zf.writestr("manifesto.json", json.dumps(manifesto, ensure_ascii=False, indent=2))
        self.zf.close()


def coletar_codex(c: Coletor, home: Path, idx: int) -> int:
    pre = "codex" if idx == 0 else f"codex_{idx}"
    n0 = c.contagem["conversa"]
    for sub in ("sessions", "archived_sessions"):
        d = home / sub
        if d.is_dir():
            for arq in sorted(d.rglob("*.jsonl")):
                c.add(arq, f"{pre}/{sub}/{arq.relative_to(d).as_posix()}", "conversa")
    for nome in ("config.toml", "AGENTS.md", "history.jsonl", "version.json"):
        if (home / nome).is_file():
            c.add(home / nome, f"{pre}/{nome}", "config")
    log = home / "log"
    if log.is_dir():
        for arq in log.glob("*.log"):
            c.add(arq, f"{pre}/log/{arq.name}", "log")
    listagem = {}
    for sub in ("skills", "plugins", "agents", "rules"):
        d = home / sub
        if d.is_dir():
            listagem[sub] = sorted(p.name for p in d.iterdir())[:300]
    c.zf.writestr(f"{pre}/listagem.json", json.dumps(listagem, ensure_ascii=False, indent=2))
    return c.contagem["conversa"] - n0


def coletar_claude(c: Coletor, home: Path, idx: int) -> int:
    pre = "claude" if idx == 0 else f"claude_{idx}"
    n0 = c.contagem["conversa"]
    proj = home / "projects"
    if proj.is_dir():
        for arq in sorted(proj.rglob("*.jsonl")):
            c.add(arq, f"{pre}/projects/{arq.relative_to(proj).as_posix()}", "conversa")
    for nome in ("settings.json", "CLAUDE.md"):
        if (home / nome).is_file():
            c.add(home / nome, f"{pre}/{nome}", "config")
    return c.contagem["conversa"] - n0


def http_json(url: str, corpo: dict, timeout: int = 60) -> dict:
    dados = json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(url, data=dados, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except ValueError:
            return {"ok": False, "error": f"http {e.code}"}
    except urllib.error.URLError as e:
        if "CERTIFICATE" in str(e).upper() and shutil.which("curl"):
            r = subprocess.run(
                ["curl", "-sS", "-X", "POST", "-H", "Content-Type: application/json", "--data-binary", "@-", url],
                input=dados, capture_output=True, timeout=timeout,
            )
            return json.loads(r.stdout.decode("utf-8") or "{}")
        raise


def enviar_arquivo(signed_url: str, arq: Path) -> tuple[bool, str]:
    tam = arq.stat().st_size
    try:
        with arq.open("rb") as f:
            req = urllib.request.Request(
                signed_url, data=f, method="PUT",
                headers={"Content-Type": "application/zip", "Content-Length": str(tam), "x-upsert": "false"},
            )
            with urllib.request.urlopen(req, timeout=1800) as r:
                return 200 <= r.status < 300, f"http {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
    except urllib.error.URLError as e:
        if shutil.which("curl"):
            r = subprocess.run(
                ["curl", "-sS", "-f", "-X", "PUT", "-H", "Content-Type: application/zip", "-H", "x-upsert: false",
                 "--data-binary", f"@{arq}", signed_url],
                capture_output=True, timeout=1800,
            )
            return r.returncode == 0, r.stderr.decode("utf-8", "replace")[:200] or "curl ok"
        return False, str(e)[:200]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Coleta do Mestre das IAs")
    ap.add_argument("--sem-enviar", action="store_true", help="so gera o zip")
    ap.add_argument("--pasta", help="pasta da empresa (Casa)")
    ap.add_argument("--saida", help="pasta onde salvar o zip")
    args = ap.parse_args()

    avisar(f"Coleta do Mestre das IAs v{VERSAO}")
    codex_homes = candidatos_home("CODEX_HOME", ".codex")
    claude_homes = candidatos_home("CLAUDE_CONFIG_DIR", ".claude")
    busca_extra = []
    if not any((h / "sessions").is_dir() for h in codex_homes) and not claude_homes:
        avisar("Nao achei as pastas padrao do Codex nem do Claude. Procurando (ate 20s)...")
        busca_extra = busca_curta_rollouts()
        codex_homes += [p for p in busca_extra if p not in codex_homes]

    cwds = Counter()
    for h in codex_homes:
        for sub in ("sessions", "archived_sessions"):
            if (h / sub).is_dir():
                for arq in (h / sub).rglob("rollout-*.jsonl"):
                    cw = cwd_da_sessao(arq)
                    if cw:
                        cwds[cw] += 1

    casa = achar_casa(args.pasta, cwds)
    ident = ler_identidade(casa)
    casa_ok = bool(casa and eh_casa(casa))

    agora = dt.datetime.now()
    saida_dir = Path(args.saida).expanduser() if args.saida else (casa if casa else Path.cwd())
    try:
        teste = saida_dir / ".coleta_teste"
        teste.write_text("ok")
        teste.unlink()
    except OSError:
        saida_dir = Path.cwd()
    zip_path = saida_dir / f"coleta-mestre-{agora:%Y%m%d-%H%M%S}.zip"

    c = Coletor(zip_path)
    n_codex = sum(coletar_codex(c, h, i) for i, h in enumerate(codex_homes))
    n_claude = sum(coletar_claude(c, h, i) for i, h in enumerate(claude_homes))
    if casa:
        avisar(f"Pasta da empresa: {casa}")
        c.add_arvore(casa, "casa", "casa")

    manifesto = {
        "versao": VERSAO,
        "gerado_em": agora.isoformat(timespec="seconds"),
        "sistema": platform.platform(),
        "python": sys.version.split()[0],
        "maquina": platform.machine(),
        "cpus": os.cpu_count(),
        "casa": str(casa) if casa else None,
        "casa_reconhecida": casa_ok,
        "identidade": ident,
        "codex_pastas": [str(p) for p in codex_homes],
        "claude_pastas": [str(p) for p in claude_homes],
        "busca_extra": [str(p) for p in busca_extra],
        "conversas_codex": n_codex,
        "conversas_claude": n_claude,
        "pastas_das_conversas": dict(cwds.most_common(30)),
    }
    c.fechar(manifesto)
    tam = zip_path.stat().st_size

    avisar("")
    avisar(f"Conversas do Codex: {n_codex} | do Claude: {n_claude}")
    avisar(f"Arquivos da empresa: {c.contagem['casa']} | segredos trocados por [REMOVIDO]: {c.removidos}")
    avisar(f"Zip: {zip_path} ({tam / 1048576:.1f} MB)")
    if n_codex + n_claude == 0:
        avisar("AVISO: nenhuma conversa encontrada. O zip vai so com a pasta da empresa.")
    if not ident["email_dono"]:
        avisar("AVISO: nao achei o e-mail do dono na pasta. O envio vai como nao identificado.")

    if args.sem_enviar:
        avisar("Modo --sem-enviar: zip gerado, nada enviado.")
        return 0

    plano_b = (
        f"\nNAO FOI POSSIVEL ENVIAR. Mande este arquivo no WhatsApp do Polozi (no privado):\n  {zip_path}"
    )
    if tam > MAX_ZIP:
        avisar(f"O zip passou de {MAX_ZIP // 1048576} MB." + plano_b)
        return 2
    try:
        avisar("Pedindo link de envio...")
        resp = http_json(ENDPOINT, {**ident, "tamanho": tam, "versao": VERSAO})
        if not resp.get("ok"):
            avisar(f"Servidor recusou: {resp.get('error')}" + plano_b)
            return 2
        avisar("Enviando...")
        ok, info = enviar_arquivo(resp["signed_url"], zip_path)
        if not ok:
            avisar(f"Falha no envio: {info}" + plano_b)
            return 2
    except Exception as e:  # rede, sandbox, proxy
        avisar(f"Erro de rede: {e.__class__.__name__}: {str(e)[:200]}" + plano_b)
        return 2

    quem = "identificado" if resp.get("identificado") else "nao identificado (o Polozi confere pelo nome)"
    avisar(f"\nENVIADO com sucesso ({quem}). Obrigado! Pode apagar o zip: {zip_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
