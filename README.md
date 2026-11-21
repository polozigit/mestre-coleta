# Coleta do Mestre das IAs

Script que junta as conversas do Codex (ou do Claude) e a pasta da sua empresa num zip e envia para o Polozi.
Serve para ele estudar onde cada aluno travou e melhorar o curso e o kit.

## Como rodar

Abra uma conversa nova do Codex **dentro da pasta da sua empresa** e cole:

> Baixe https://raw.githubusercontent.com/polozigit/mestre-coleta/main/coletar.py e rode com Python 3 aqui na pasta da minha empresa (Mac: `curl -fsSL <link> -o coletar.py && python3 coletar.py`; Windows: `curl.exe -fsSL <link> -o coletar.py; python coletar.py`). Se pedir permissão para usar a rede ou ler fora da pasta, aprove. No fim, me mostre a última linha.

## O que vai e o que não vai

Vai: conversas do Codex e do Claude Code, configuração do Codex (sem chaves), a pasta da empresa com o histórico do git.

Não vai: pasta `credenciais/`, arquivos `.env`, chaves (`.pem`, `.key`), `node_modules`, arquivos acima de 25 MB.
Chaves de API e tokens que aparecem dentro dos textos são trocados por `[REMOVIDO]` antes de gerar o zip.

O zip fica salvo na pasta da empresa (`coleta-mestre-AAAAMMDD-HHMMSS.zip`). Se o envio falhar, o script avisa:
mande esse arquivo no WhatsApp do Polozi, no privado.
