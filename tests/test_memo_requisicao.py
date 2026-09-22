"""O memo por requisição: uma leitura por requisição, nenhuma fora dela."""
from django.http import HttpResponse

from core.memo_requisicao import MemoRequisicaoMiddleware, esquecer, lembrar


class _Contador:
    def __init__(self):
        self.leituras = 0

    def __call__(self):
        self.leituras += 1
        return f"valor-{self.leituras}"


def _dentro_de_requisicao(corpo):
    return MemoRequisicaoMiddleware(lambda _request: corpo() or HttpResponse())(object())


def test_fora_de_requisicao_toda_leitura_vai_a_fonte():
    carregar = _Contador()

    assert [lembrar("chave", carregar) for _ in range(3)] == ["valor-1", "valor-2", "valor-3"]


def test_dentro_da_requisicao_a_fonte_e_lida_uma_vez():
    carregar = _Contador()
    vistos = []

    _dentro_de_requisicao(lambda: vistos.extend(lembrar("chave", carregar) for _ in range(5)))

    assert vistos == ["valor-1"] * 5
    assert carregar.leituras == 1


def test_esquecer_obriga_a_ler_de_novo_na_mesma_requisicao():
    carregar = _Contador()
    vistos = []

    def corpo():
        vistos.append(lembrar("chave", carregar))
        esquecer("chave")
        vistos.append(lembrar("chave", carregar))

    _dentro_de_requisicao(corpo)

    assert vistos == ["valor-1", "valor-2"]


def test_o_memo_nao_passa_de_uma_requisicao_para_a_outra():
    carregar = _Contador()
    vistos = []

    _dentro_de_requisicao(lambda: vistos.append(lembrar("chave", carregar)))
    _dentro_de_requisicao(lambda: vistos.append(lembrar("chave", carregar)))

    assert vistos == ["valor-1", "valor-2"]
    # E, terminada a requisição, o memo fechou: a leitura volta a ir à fonte.
    assert lembrar("chave", carregar) == "valor-3"
