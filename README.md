# Coleta do Mestre das IAs

Script que junta as conversas do Codex (ou do Claude) e a pasta da sua empresa num zip e envia para o Polozi.
Serve para ele estudar onde cada aluno travou e melhorar o curso e o kit.

## Como rodar

Abra uma conversa nova do Codex **dentro da pasta da sua empresa** e cole:

> Baixe https://raw.githubusercontent.com/polozigit/mestre-coleta/main/coletar.py e rode com Python 3 aqui na pasta da minha empresa (Mac: `curl -fsSL <link> -o coletar.py && python3 coletar.py`; Windows: `curl.exe -fsSL <link> -o coletar.py; python coletar.py`). Se pedir permissão para usar a rede ou ler fora da pasta, aprove. No fim, me mostre a última linha.

## Coleta leve (régua da turma)

`--leve` manda só o que mostra a execução: conversas enxutas, árvore de arquivos da pasta (sem conteúdo),
registros de `operacao/`, config e hooks do Codex, histórico de commits e versões dos programas (Python, Node, gh...).
Fica em poucos MB. No fim o script imprime `TRABALHO CONCLUIDO`: a conversa pode ser encerrada e arquivada.

Quando o comando vem na mensagem individual do WhatsApp, ele traz `--aluno <código>`: é o que liga o envio a você, sem depender do e-mail na pasta.

## O que vai e o que não vai

Vai: conversas do Codex e do Claude Code (enxutas: sem imagens, sem cópias duplicadas, saídas gigantes cortadas),
configuração do Codex (sem chaves), a pasta da empresa e o histórico de commits em texto.

Não vai: pasta `credenciais/`, arquivos `.env`, chaves (`.pem`, `.key`), `node_modules`, a pasta `.git`,
`organograma.json`, arquivos acima de 5 MB.
Chaves de API e tokens que aparecem dentro dos textos são trocados por `[REMOVIDO]` antes de gerar o zip.
Zip acima de 40 MB é enviado em partes.

O zip fica salvo na pasta da empresa (`coleta-mestre-AAAAMMDD-HHMMSS.zip`). Se o envio falhar, o script avisa:
mande esse arquivo no WhatsApp do Polozi, no privado.
