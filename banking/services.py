"""Serviços de Cadastros: Instituições e Contas financeiras."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError
from django.db.models import Exists, OuterRef, ProtectedError
from django.utils.timezone import localdate

from accounts.services import accessible_owner_ids, can_access_owner
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    ACCOUNT_KIND_REGULAR,
    BASE_CURRENCY,
    CARD_DAY_MAX,
    CARD_DAY_MIN,
    VALID_ACCOUNT_KINDS,
    VALID_CURRENCIES,
    MixedCurrencyError,
)

from .models import FinancialAccount, FinancialInstitution

_MAX_NAME_LENGTH = 100
_VALID_INSTITUTION_TYPES = ("Banco", "Corretora")


# --- Instituições ---

def list_institutions(institution_type: str | None = None):
    queryset = FinancialInstitution.objects.all()
    if institution_type:
        queryset = queryset.filter(institution_type=institution_type)
    return queryset


def _clean_institution_fields(name: str, institution_type: str) -> tuple[str, str]:
    name = (name or "").strip()
    if not name:
        raise ValueError("Nome da instituição é obrigatório.")
    if len(name) > _MAX_NAME_LENGTH:
        raise ValueError(f"Nome da instituição não pode exceder {_MAX_NAME_LENGTH} caracteres.")

    institution_type = (institution_type or "Banco").strip().capitalize()
    if institution_type not in _VALID_INSTITUTION_TYPES:
        raise ValueError("Tipo de instituição inválido.")

    return name, institution_type


def create_institution(name: str, institution_type: str) -> FinancialInstitution:
    clean_name, clean_type = _clean_institution_fields(name, institution_type)
    if FinancialInstitution.objects.filter(institution_name__iexact=clean_name).exists():
        raise ValueError("Já existe uma instituição com esse nome.")
    try:
        return FinancialInstitution.objects.create(
            institution_name=clean_name, institution_type=clean_type
        )
    except IntegrityError as exc:
        raise ValueError("Já existe uma instituição com esse nome.") from exc


def update_institution(institution: FinancialInstitution, name: str, institution_type: str) -> FinancialInstitution:
    clean_name, clean_type = _clean_institution_fields(name, institution_type)
    if FinancialInstitution.objects.filter(institution_name__iexact=clean_name).exclude(id=institution.id).exists():
        raise ValueError("Já existe uma instituição com esse nome.")
    institution.institution_name = clean_name
    institution.institution_type = clean_type
    try:
        institution.save(update_fields=["institution_name", "institution_type", "updated_at"])
    except IntegrityError as exc:
        raise ValueError("Já existe uma instituição com esse nome.") from exc
    return institution


def delete_institution(institution: FinancialInstitution) -> None:
    try:
        institution.delete()
    except ProtectedError as exc:
        raise ValueError(
            "Não é possível excluir esta instituição: existem contas vinculadas a ela."
        ) from exc


# --- Contas ---

def accessible_account_ids(user, action: str = "view") -> list[int]:
    """IDs de contas que `user` pode acessar para a ação informada.

    Contas não têm controle de acesso próprio: o escopo é herdado do
    titular (`AccountOwner`) a que pertencem, via `accessible_owner_ids`.
    Helper central para qualquer módulo (Cadastros, Transações, Bancos) que
    precise restringir consultas a contas visíveis ao usuário.
    """
    owner_ids = accessible_owner_ids(user, action)
    if not owner_ids:
        return []
    return list(FinancialAccount.objects.filter(owner_id__in=owner_ids).values_list('id', flat=True))


def can_access_account(user, account_id, action: str = "view") -> bool:
    """True se `user` pode acessar a conta `account_id` para a ação informada."""
    if not account_id:
        return False
    try:
        account = FinancialAccount.objects.only('owner_id').get(id=account_id)
    except FinancialAccount.DoesNotExist:
        return False
    return can_access_owner(user, account.owner_id, action)


def list_accounts_for_user(user, owner_id: int | None = None, institution_id: int | None = None):
    """Contas visíveis para `user`, restritas aos titulares acessíveis.

    `has_entries` acompanha cada conta porque a moeda de conta com lançamento é
    imutável (ver `update_account`): a tela usa isso para não oferecer uma troca
    que o servidor vai recusar. O `Exists` percorre a relação inversa
    `transactions` sem importar o app de lançamentos.
    """
    owner_ids = accessible_owner_ids(user, "view")
    queryset = FinancialAccount.objects.select_related('owner', 'institution', 'card_payment_account').filter(
        owner_id__in=owner_ids
    ).annotate(
        has_entries=Exists(
            FinancialAccount.objects.filter(pk=OuterRef('pk'), transactions__isnull=False)
        )
    )
    if owner_id:
        queryset = queryset.filter(owner_id=owner_id)
    if institution_id:
        queryset = queryset.filter(institution_id=institution_id)
    return queryset


def _parse_initial_balance(raw_value: str | None) -> Decimal:
    if raw_value is None or str(raw_value).strip() == "":
        return Decimal("0.00")
    try:
        return Decimal(str(raw_value).strip().replace(',', '.'))
    except InvalidOperation as exc:
        raise ValueError("Saldo inicial inválido.") from exc


def currencies_of_accounts(account_ids) -> tuple[str, ...]:
    """Moedas distintas de um conjunto de contas, em ordem."""
    ids = [int(account_id) for account_id in account_ids if account_id]
    if not ids:
        return ()
    # `order_by()` sem argumento limpa a ordenação padrão do modelo
    # (`account_name`). Sem isso o Django acrescenta essa coluna ao SELECT, o
    # `DISTINCT` passa a valer para o par (moeda, nome) e duas contas na mesma
    # moeda voltam como duas moedas -- foi assim que a primeira versão desta
    # função acusou moedas misturadas onde só havia real.
    return tuple(sorted(
        FinancialAccount.objects.filter(id__in=ids)
        .order_by()
        .values_list("currency", flat=True)
        .distinct()
    ))


def currency_of_accounts(account_ids) -> str:
    """A moeda comum do conjunto, ou `MixedCurrencyError`.

    Conjunto vazio vale a moeda base: uma tela sem conta nenhuma continua
    escrevendo `R$ 0,00`, que é o que ela sempre escreveu.

    É a porta única por onde todo agregado que atravessa contas passa. Sem ela,
    um total mudo somaria real com dólar e pareceria certo -- o defeito mais
    caro que esta mudança podia deixar para trás.
    """
    currencies = currencies_of_accounts(account_ids)
    if not currencies:
        return BASE_CURRENCY
    if len(currencies) > 1:
        raise MixedCurrencyError(currencies)
    return currencies[0]


def account_ids_by_currency(account_ids) -> dict[str, list[int]]:
    """As contas da seleção, repartidas por moeda.

    É a porta por onde as telas pedem um bloco de totais para cada moeda, em vez
    de um total só para a seleção inteira. Cada grupo que sai daqui é de uma
    moeda só, então os agregados continuam recebendo o que sempre exigiram --
    e `currency_of_accounts` segue sendo a rede para quem esquecer disso.

    Moeda base primeiro, depois alfabética: a ordem dos blocos na tela não pode
    depender da ordem em que o banco devolveu as linhas.
    """
    ids = [int(account_id) for account_id in account_ids if account_id]
    if not ids:
        return {}
    agrupadas: dict[str, list[int]] = {}
    for account_id, currency in (
        FinancialAccount.objects.filter(id__in=ids)
        .order_by("account_name", "id")
        .values_list("id", "currency")
    ):
        agrupadas.setdefault(currency, []).append(account_id)
    return {
        currency: agrupadas[currency]
        for currency in sorted(agrupadas, key=lambda c: (c != BASE_CURRENCY, c))
    }


def currency_blocks(account_ids, currency_filter: str | None = None) -> list[tuple[str, list[int]]]:
    """Os grupos de `account_ids_by_currency`, sempre com pelo menos um bloco.

    Seleção sem conta nenhuma continua rendendo um bloco em moeda base: a tela
    vazia escreve `R$ 0,00` como sempre escreveu, em vez de sumir.
    """
    grupos = account_ids_by_currency(account_ids)
    if currency_filter and currency_filter != "ALL":
        grupos = {currency: ids for currency, ids in grupos.items() if currency == currency_filter}
    if not grupos:
        empty_currency = currency_filter if currency_filter and currency_filter != "ALL" else BASE_CURRENCY
        return [(empty_currency, [])]
    return list(grupos.items())


def _parse_currency(raw_value: str | None) -> str:
    currency = (raw_value or "").strip().upper()
    if not currency:
        raise ValueError("Moeda é obrigatória.")
    if currency not in VALID_CURRENCIES:
        raise ValueError(f"Moeda inválida: {currency}.")
    return currency


def _parse_initial_balance_date(raw_value: str | None) -> date:
    """Vazio vale como hoje; conta nova sem data informada é conta de hoje.

    O histórico já foi datado em 31/12/2025 pela migração
    `0004_conta_moeda_e_data_do_saldo_inicial`, e não é o formulário que
    reescreve isso.
    """
    raw_text = (raw_value or "").strip()
    if not raw_text:
        return localdate()
    try:
        return date.fromisoformat(raw_text)
    except ValueError as exc:
        raise ValueError("Data do saldo inicial inválida.") from exc


def _clean_account_fields(owner_id: str, institution_id: str, account_name: str, initial_balance: str):
    try:
        owner_id_int = int(owner_id)
        if owner_id_int <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Titular é obrigatório.") from exc

    try:
        institution_id_int = int(institution_id)
        if institution_id_int <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Instituição é obrigatória.") from exc

    account_name = (account_name or "").strip()
    if not account_name:
        raise ValueError("Nome da conta é obrigatório.")
    if len(account_name) > _MAX_NAME_LENGTH:
        raise ValueError(f"Nome da conta não pode exceder {_MAX_NAME_LENGTH} caracteres.")

    balance = _parse_initial_balance(initial_balance)

    return owner_id_int, institution_id_int, account_name, balance


def _parse_card_day(raw_value: str | None, label: str) -> int:
    try:
        day = int((raw_value or "").strip())
    except ValueError as exc:
        raise ValueError(f"{label} do cartão é obrigatório (1 a 31).") from exc
    if not CARD_DAY_MIN <= day <= CARD_DAY_MAX:
        raise ValueError(f"{label} do cartão tem de estar entre 1 e 31.")
    return day


def _parse_estimated_spend(raw_value: str | None) -> Decimal | None:
    """Gasto novo por fatura fixado à mão; vazio deixa a mediana decidir."""
    raw_text = (raw_value or "").strip()
    if not raw_text:
        return None
    if "," in raw_text:
        raw_text = raw_text.replace(".", "").replace(",", ".")
    try:
        valor = Decimal(raw_text).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("Gasto mensal estimado do cartão inválido.") from exc
    if valor < 0:
        raise ValueError("Gasto mensal estimado do cartão não pode ser negativo.")
    return valor


def _clean_card_fields(
    user,
    *,
    account_kind: str,
    card_closing_day: str,
    card_due_day: str,
    card_payment_account_id: str,
    card_estimated_spend: str = "",
    currency: str,
    account_id: int | None = None,
) -> dict:
    """Os campos de cartão, validados. Conta comum sai com todos vazios.

    A conta de pagamento padrão é opcional: a fatura pode ser paga de mais de
    uma conta, com uma transferência de cada. Ela só diz de onde a fatura
    projetada sai, e por isso tem de ser uma conta comum, na mesma moeda, que
    o usuário enxerga.
    """
    kind = (account_kind or ACCOUNT_KIND_REGULAR).strip()
    if kind not in VALID_ACCOUNT_KINDS:
        raise ValueError("Tipo de conta inválido.")
    if kind == ACCOUNT_KIND_REGULAR:
        return {
            "account_kind": kind,
            "card_closing_day": None,
            "card_due_day": None,
            "card_payment_account": None,
            "card_estimated_spend": None,
        }
    payment = None
    raw_payment = (card_payment_account_id or "").strip()
    if raw_payment:
        try:
            payment_id = int(raw_payment)
        except ValueError as exc:
            raise ValueError("Conta de pagamento inválida.") from exc
        payment = FinancialAccount.objects.filter(id=payment_id).first()
        if payment is None or not can_access_account(user, payment_id):
            raise ValueError("Conta de pagamento não encontrada.")
        if payment.id == account_id:
            raise ValueError("O cartão não pode pagar a própria fatura.")
        if payment.account_kind == ACCOUNT_KIND_CREDIT_CARD:
            raise ValueError("A conta de pagamento tem de ser uma conta comum, não outro cartão.")
        if payment.currency != currency:
            raise ValueError("A conta de pagamento tem de estar na mesma moeda do cartão.")
    return {
        "account_kind": kind,
        "card_closing_day": _parse_card_day(card_closing_day, "Dia de fechamento"),
        "card_due_day": _parse_card_day(card_due_day, "Dia de vencimento"),
        "card_payment_account": payment,
        "card_estimated_spend": _parse_estimated_spend(card_estimated_spend),
    }


def create_account(
    user,
    *,
    owner_id: str,
    institution_id: str,
    account_name: str,
    initial_balance: str,
    currency: str,
    initial_balance_date: str = "",
    account_kind: str = ACCOUNT_KIND_REGULAR,
    card_closing_day: str = "",
    card_due_day: str = "",
    card_payment_account_id: str = "",
    card_estimated_spend: str = "",
) -> FinancialAccount:
    clean_owner_id, clean_institution_id, clean_name, balance = _clean_account_fields(
        owner_id, institution_id, account_name, initial_balance
    )
    clean_currency = _parse_currency(currency)
    clean_balance_date = _parse_initial_balance_date(initial_balance_date)
    if not can_access_owner(user, clean_owner_id, "create"):
        raise ValueError("Acesso negado: você não pode criar contas para este titular.")
    if not FinancialInstitution.objects.filter(id=clean_institution_id).exists():
        raise ValueError("Instituição não encontrada.")
    card = _clean_card_fields(
        user,
        account_kind=account_kind,
        card_closing_day=card_closing_day,
        card_due_day=card_due_day,
        card_payment_account_id=card_payment_account_id,
        card_estimated_spend=card_estimated_spend,
        currency=clean_currency,
    )

    return FinancialAccount.objects.create(
        owner_id=clean_owner_id,
        institution_id=clean_institution_id,
        account_name=clean_name,
        initial_balance=balance,
        currency=clean_currency,
        initial_balance_date=clean_balance_date,
        **card,
    )


def update_account(
    user,
    account: FinancialAccount,
    *,
    owner_id: str,
    institution_id: str,
    account_name: str,
    initial_balance: str,
    currency: str,
    initial_balance_date: str = "",
    account_kind: str = ACCOUNT_KIND_REGULAR,
    card_closing_day: str = "",
    card_due_day: str = "",
    card_payment_account_id: str = "",
    card_estimated_spend: str = "",
) -> FinancialAccount:
    clean_owner_id, clean_institution_id, clean_name, balance = _clean_account_fields(
        owner_id, institution_id, account_name, initial_balance
    )
    clean_currency = _parse_currency(currency)
    clean_balance_date = _parse_initial_balance_date(initial_balance_date)
    if not can_access_owner(user, account.owner_id, "update"):
        raise ValueError("Acesso negado: você não pode alterar contas deste titular.")
    if not can_access_owner(user, clean_owner_id, "update"):
        raise ValueError("Acesso negado: você não pode transferir a conta para este titular.")
    if not FinancialInstitution.objects.filter(id=clean_institution_id).exists():
        raise ValueError("Instituição não encontrada.")
    # Trocar a moeda de uma conta com lançamento reescreveria em silêncio o
    # significado de todo o passado dela: os mesmos valores passariam a valer
    # em outra moeda, sem nenhum registro de conversão. Conta com histórico não
    # muda de moeda -- se a moeda estiver errada, a saída é outra conta.
    if clean_currency != account.currency and account.transactions.exists():
        raise ValueError(
            "Esta conta já tem lançamentos e não pode mudar de moeda: os valores "
            "já registrados continuariam iguais, valendo outra coisa. Crie uma "
            "conta na moeda correta e transfira o saldo."
        )

    card = _clean_card_fields(
        user,
        account_kind=account_kind,
        card_closing_day=card_closing_day,
        card_due_day=card_due_day,
        card_payment_account_id=card_payment_account_id,
        card_estimated_spend=card_estimated_spend,
        currency=clean_currency,
        account_id=account.id,
    )
    # Uma conta que paga algum cartão não pode virar cartão: o cartão dela
    # passaria a ter outro cartão como conta de pagamento.
    if card["account_kind"] == ACCOUNT_KIND_CREDIT_CARD and account.cards_paid.exists():
        raise ValueError(
            "Esta conta é a conta de pagamento de um cartão e não pode virar cartão. "
            "Troque antes a conta de pagamento desse cartão."
        )

    account.owner_id = clean_owner_id
    account.institution_id = clean_institution_id
    account.account_name = clean_name
    account.initial_balance = balance
    account.currency = clean_currency
    account.initial_balance_date = clean_balance_date
    for field, value in card.items():
        setattr(account, field, value)
    account.save(update_fields=[
        "owner", "institution", "account_name", "initial_balance",
        "currency", "initial_balance_date", *card, "updated_at",
    ])
    return account


def delete_account(user, account: FinancialAccount) -> None:
    if not can_access_owner(user, account.owner_id, "delete"):
        raise ValueError("Acesso negado: você não pode excluir contas deste titular.")
    try:
        account.delete()
    except ProtectedError as exc:
        raise ValueError(
            "Não é possível excluir esta conta: existem lançamentos vinculados a ela."
        ) from exc
