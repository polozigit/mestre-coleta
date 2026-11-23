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

VERSAO = "1.3"
LEVE = False  # --leve: so o que mostra a execucao (padrao da regua)
RE_ALUNO = re.compile(r"[A-Za-z0-9_-]{22}")  # lid da inscricao (lid_compacto no banco)
ENDPOINT = "https://lgpagqxsxdsepxjmlnhc.supabase.co/functions/v1/mestre-coleta-upload"
MAX_ARQUIVO = 5 * 1024 * 1024
MAX_ZIP = 480 * 1024 * 1024
TAM_PARTE = 40 * 1024 * 1024
MAX_LINHA = 40000
ARQ_CASA_FORA = {"organograma.json"}
BASE64_RE = re.compile(rb"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]{200,}|[A-Za-z0-9+/=_\-]{2000,}")
ENC_RE = re.compile(rb'"encrypted_content"\s*:\s*"[^"]*"')
DUPLICADA_RE = re.compile(rb'"type"\s*:\s*"event_msg".{0,200}?"type"\s*:\s*"item_completed"')

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
        self.zf = zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False)
        self.pulados: list[dict] = []
        self.contagem: Counter = Counter()
        self.removidos = 0

    def add_conversa(self, origem: Path, destino: str) -> None:
        """Le linha a linha (cabe conversa de centenas de MB): tira copia duplicada, imagem/base64,
        raciocinio criptografado e corta saida gigante, guardando comeco e fim."""
        try:
            with origem.open("rb") as f, self.zf.open(destino, "w", force_zip64=True) as out:
                for linha in f:
                    if DUPLICADA_RE.search(linha[:400]):
                        continue
                    linha = ENC_RE.sub(b'"encrypted_content":""', linha)
                    linha = BASE64_RE.sub(b"[IMAGEM_OU_BASE64]", linha)
                    if len(linha) > MAX_LINHA:
                        linha = linha[:20000] + b"...[CORTADO]..." + linha[-5000:]
                    texto, n = redigir(linha.decode("utf-8", "replace"))
                    self.removidos += n
                    out.write(texto.encode("utf-8"))
            self.contagem["conversa"] += 1
        except OSError as e:
            self.pulados.append({"arquivo": destino, "motivo": f"erro: {e.__class__.__name__}"})

    def add(self, origem: Path, destino: str, categoria: str, redigir_texto: bool = True) -> None:
        if categoria == "conversa":
            self.add_conversa(origem, destino)
            return
        try:
            tam = origem.stat().st_size
        except OSError as e:
            self.pulados.append({"arquivo": destino, "motivo": f"erro: {e.__class__.__name__}"})
            return
        if tam > MAX_ARQUIVO:
            self.pulados.append({"arquivo": destino, "motivo": f"grande ({tam / 1048576:.1f} MB)"})
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

    def add_arvore(self, raiz: Path, prefixo: str, categoria: str) -> None:
        for dirpath, dirs, arqs in os.walk(raiz):
            dirs[:] = [d for d in dirs if d not in DIRS_FORA and d != ".git"]
            rel_dir = Path(dirpath).relative_to(raiz)
            for a in arqs:
                if a in ARQ_CASA_FORA:
                    self.pulados.append({"arquivo": f"{prefixo}/{(rel_dir / a).as_posix()}", "motivo": "ja temos no kit"})
                    continue
                if ARQ_FORA_RE.search(a):
                    self.pulados.append({"arquivo": f"{prefixo}/{(rel_dir / a).as_posix()}", "motivo": "sensivel"})
                    continue
                origem = Path(dirpath) / a
                if origem.is_symlink():
                    continue
                self.add(origem, f"{prefixo}/{(rel_dir / a).as_posix()}", categoria)

    def add_casa_leve(self, casa: Path) -> None:
        """So o que mostra a execucao: arvore de arquivos (sem conteudo), registros da operacao,
        config e hooks do Codex, AGENTS.md. Nada de imagem, codigo ou sistema."""
        linhas = []
        for dirpath, dirs, arqs in os.walk(casa):
            dirs[:] = [d for d in dirs if d not in DIRS_FORA and d != ".git"]
            rel_dir = Path(dirpath).relative_to(casa)
            for a in arqs:
                try:
                    tam = (Path(dirpath) / a).stat().st_size
                except OSError:
                    tam = -1
                linhas.append(f"{tam}\t{(rel_dir / a).as_posix()}")
        self.zf.writestr("casa_arvore.txt", "\n".join(sorted(linhas)[:20000]))
        alvos = [casa / "AGENTS.md", casa / ".codex" / "config.toml", casa / ".codex" / "hooks.json"]
        op = casa / "operacao"
        if op.is_dir():
            alvos += [x for x in op.rglob("*.md") if ".sessoes" not in x.parts]
            alvos += [x for x in (op / "telemetria.jsonl", op / "tasks" / ".sessoes" / "casa.json") if x.exists()]
        for x in alvos:
            if x.is_file() and not ARQ_FORA_RE.search(x.name):
                self.add(x, f"casa/{x.relative_to(casa).as_posix()}", "casa")

    def add_historico_git(self, casa: Path) -> None:
        """No lugar da pasta .git (pesada): o historico de commits em texto."""
        if not (casa / ".git").exists():
            return
        texto = ""
        if shutil.which("git"):
            try:
                r = subprocess.run(
                    ["git", "-C", str(casa), "-c", "core.fsmonitor=", "log", "--all", "--stat", "--date=iso", "-n", "3000"],
                    capture_output=True, timeout=60,
                )
                texto = r.stdout.decode("utf-8", "replace")[: 3 * 1024 * 1024]
            except (OSError, subprocess.SubprocessError):
                texto = ""
        if not texto:
            head = casa / ".git" / "logs" / "HEAD"
            if head.is_file():
                texto = head.read_text(encoding="utf-8", errors="replace")[: 3 * 1024 * 1024]
        if texto:
            texto, n = redigir(texto)
            self.removidos += n
            self.zf.writestr("casa_git_historico.txt", texto)

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
    for nome in (("config.toml", "AGENTS.md", "version.json") if LEVE else ("config.toml", "AGENTS.md", "history.jsonl", "version.json")):
        if (home / nome).is_file():
            c.add(home / nome, f"{pre}/{nome}", "config")
    log = home / "log"
    if log.is_dir() and not LEVE:
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


def id_maquina() -> str:
    """Id anonimo e estavel da maquina: separa alunos sem e-mail na Casa."""
    import hashlib
    base = f"{platform.node()}|{Path.home()}"
    return hashlib.sha256(base.encode("utf-8", "replace")).hexdigest()[:12]


def medir_ambiente() -> dict:
    """Versao dos programas que o kit usa (o que mais travou na turma 19/11)."""
    progs = {"python3": ["python3", "--version"], "py": ["py", "-3", "--version"], "python": ["python", "--version"],
             "node": ["node", "--version"], "npm": ["npm", "--version"], "git": ["git", "--version"],
             "gh": ["gh", "--version"], "supabase": ["supabase", "--version"], "sh": ["sh", "-c", "echo ok"]}
    out = {}
    for nome, cmd in progs.items():
        exe = shutil.which(cmd[0])
        if not exe:
            out[nome] = None
            continue
        try:
            r = subprocess.run([exe, *cmd[1:]], capture_output=True, timeout=8)
            out[nome] = (r.stdout or r.stderr).decode("utf-8", "replace").strip().splitlines()[:1] or [f"exit {r.returncode}"]
        except (OSError, subprocess.SubprocessError) as e:
            out[nome] = [f"erro {e.__class__.__name__}"]
    return out


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
    ap.add_argument("--leve", action="store_true", help="so o que mostra a execucao (conversas, registros, ambiente)")
    ap.add_argument("--aluno", help="codigo do aluno que veio na mensagem do WhatsApp (identifica o envio)")
    args = ap.parse_args()
    aluno = (args.aluno or "").strip()
    if aluno and not RE_ALUNO.fullmatch(aluno):
        avisar(f"AVISO: codigo do aluno invalido ({aluno[:30]}). Sigo sem ele.")
        aluno = ""
    global LEVE
    LEVE = args.leve

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
        if LEVE:
            c.add_casa_leve(casa)
        else:
            c.add_arvore(casa, "casa", "casa")
        c.add_historico_git(casa)
    c.zf.writestr("ambiente.json", json.dumps(medir_ambiente(), ensure_ascii=False, indent=2))

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
        "aluno": aluno or None,
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
    if not ident["email_dono"] and not aluno:
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
    partes = [zip_path]
    if tam > TAM_PARTE:
        partes = []
        with zip_path.open("rb") as f:
            i = 1
            while True:
                bloco = f.read(TAM_PARTE)
                if not bloco:
                    break
                p = zip_path.with_name(f"{zip_path.name}.parte{i:02d}")
                p.write_bytes(bloco)
                partes.append(p)
                i += 1
        avisar(f"Zip dividido em {len(partes)} partes de ate {TAM_PARTE // 1048576} MB.")
    try:
        resp = {}
        for n, parte in enumerate(partes, 1):
            avisar(f"Enviando parte {n} de {len(partes)}..." if len(partes) > 1 else "Enviando...")
            resp = http_json(ENDPOINT, {**ident, "tamanho": parte.stat().st_size, "versao": f"{VERSAO}{'L' if LEVE else ''} p{n}/{len(partes)}", "maquina": id_maquina(), "aluno": aluno})
            if not resp.get("ok"):
                avisar(f"Servidor recusou: {resp.get('error')}" + plano_b)
                return 2
            ok, info = enviar_arquivo(resp["signed_url"], parte)
            if not ok:
                avisar(f"Falha no envio: {info}" + plano_b)
                return 2
        for parte in partes:
            if parte != zip_path:
                parte.unlink(missing_ok=True)
    except Exception as e:  # rede, sandbox, proxy
        avisar(f"Erro de rede: {e.__class__.__name__}: {str(e)[:200]}" + plano_b)
        return 2

    quem = "identificado" if resp.get("identificado") else "nao identificado (o Polozi confere pelo nome)"
    avisar(f"\nENVIADO com sucesso ({quem}). Obrigado! O zip ficou em: {zip_path}")
    avisar("TRABALHO CONCLUIDO. Nada mais a fazer nesta conversa: encerre e arquive esta conversa.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
