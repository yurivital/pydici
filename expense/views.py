# coding: utf-8
"""
Pydici expense views. Http request are processed here.
@author: Sébastien Renard (sebastien.renard@digitalfox.org)
@license: AGPL v3 or newer (http://www.gnu.org/licenses/agpl-3.0.html)
"""

from datetime import date, timedelta
import json
import mimetypes
from cStringIO import StringIO
from base64 import b64encode

import permissions.utils as perm
import workflows.utils as wf
from workflows.models import Transition
from django_tables2 import RequestConfig

from django.http import HttpResponseRedirect, HttpResponse
from django.utils.translation import ugettext as _
from django.core import urlresolvers
from django.db.models import Q
from django.shortcuts import render, redirect
from django.contrib import messages
from core import utils

from expense.forms import ExpenseForm, ExpensePaymentForm
from expense.models import Expense, ExpensePayment
from expense.tables import ExpenseTable, UserExpenseWorkflowTable, ManagedExpenseWorkflowTable, ExpensePaymentTable
from people.models import Consultant
from staffing.models import Mission
from core.decorator import pydici_non_public, pydici_feature
from core.views import tableToCSV


@pydici_non_public
@pydici_feature("reports")
def expenses(request, expense_id=None):
    """Display user expenses and expenses that he can validate"""
    if not request.user.groups.filter(name="expense_requester").exists():
        return HttpResponseRedirect(urlresolvers.reverse("forbiden"))
    try:
        consultant = Consultant.objects.get(trigramme__iexact=request.user.username)
        user_team = consultant.userTeam(excludeSelf=False)
    except Consultant.DoesNotExist:
        user_team = []

    try:
        if expense_id:
            expense = Expense.objects.get(id=expense_id)
            if not (perm.has_permission(expense, request.user, "expense_edit")
                    and (expense.user == request.user or expense.user in user_team)):
                messages.add_message(request, messages.WARNING, _("You are not allowed to edit that expense"))
                expense_id = None
                expense = None
    except Expense.DoesNotExist:
        messages.add_message(request, messages.ERROR, _("Expense %s does not exist" % expense_id))
        expense_id = None

    if request.method == "POST":
        if expense_id:
            form = ExpenseForm(request.POST, request.FILES, instance=expense)
        else:
            form = ExpenseForm(request.POST, request.FILES)
        if form.is_valid():
            expense = form.save(commit=False)
            if not hasattr(expense, "user"):
                # Don't update user if defined (case of expense updated by manager or adminstrator)
                expense.user = request.user
            expense.creation_date = date.today()
            expense.save()
            wf.set_initial_state(expense)
            return HttpResponseRedirect(urlresolvers.reverse("expense.views.expenses"))
    else:
        if expense_id:
            form = ExpenseForm(instance=expense)  # A form that edit current expense
        else:
            form = ExpenseForm(initial={"expense_date": date.today()})  # An unbound form

    # Get user expenses
    user_expenses = Expense.objects.filter(user=request.user, workflow_in_progress=True).select_related()

    if user_team:
        team_expenses = Expense.objects.filter(user__in=user_team, workflow_in_progress=True).select_related()
    else:
        team_expenses = []

    # Paymaster manage all expenses
    if utils.has_role(request.user, "expense paymaster"):
        managed_expenses = Expense.objects.filter(workflow_in_progress=True).exclude(user=request.user).select_related()
    else:
        managed_expenses = team_expenses

    userExpenseTable = UserExpenseWorkflowTable(user_expenses)
    userExpenseTable.transitionsData = dict([(e.id, []) for e in user_expenses])  # Inject expense allowed transitions. Always empty for own expense
    userExpenseTable.expenseEditPerm = dict([(e.id, perm.has_permission(e, request.user, "expense_edit")) for e in user_expenses])  # Inject expense edit permissions
    RequestConfig(request, paginate={"per_page": 50}).configure(userExpenseTable)

    managedExpenseTable = ManagedExpenseWorkflowTable(managed_expenses)
    managedExpenseTable.transitionsData = dict([(e.id, e.transitions(request.user)) for e in managed_expenses])  # Inject expense allowed transitions
    managedExpenseTable.expenseEditPerm = dict([(e.id, perm.has_permission(e, request.user, "expense_edit")) for e in managed_expenses])  # Inject expense edit permissions
    RequestConfig(request, paginate={"per_page": 100}).configure(managedExpenseTable)

    # Prune every expense not updated since 60 days. For instance, rejected expense.
    for expense in Expense.objects.filter(workflow_in_progress=True, update_date__lt=(date.today() - timedelta(60))):
        if wf.get_state(expense).transitions.count() == 0:
            expense.workflow_in_progress = False
            expense.save()

    return render(request, "expense/expenses.html",
                  {"user_expense_table": userExpenseTable,
                   "managed_expense_table": managedExpenseTable,
                   "modify_expense": bool(expense_id),
                   "form": form,
                   "user": request.user})


@pydici_non_public
@pydici_feature("management")
def expense_receipt(request, expense_id):
    """Returns expense receipt if authorize to"""
    data = StringIO()

    try:
        expense = Expense.objects.get(id=expense_id)
        if expense.user == request.user or\
           utils.has_role(request.user, "expense paymaster") or\
           utils.has_role(request.user, "expense manager"):
            if expense.receipt:
                content_type = mimetypes.guess_type(expense.receipt.name)[0] or "application/stream"
                for chunk in expense.receipt.chunks():
                    data.write(chunk)
    except (Expense.DoesNotExist, OSError):
        pass

    data = b64encode(data.getvalue())
    if content_type=="application/pdf":
        response = "<object data='data:application/pdf;base64,%s' type='application/pdf' width='100%%' height='100%%'></object>" % data
    else:
        response = "<img src='data:%s;base64,%s'>" % (content_type, data)
    return HttpResponse(response)


@pydici_non_public
@pydici_feature("reports")
def expenses_history(request):
    """Display expense history.
    @param year: year of history. If None, display recent items and year index"""
    expenses = Expense.objects.all().select_related().prefetch_related("clientbill_set", "user", "lead")
    try:
        consultant = Consultant.objects.get(trigramme__iexact=request.user.username)
        user_team = consultant.userTeam()
    except Consultant.DoesNotExist:
        user_team = []

    if not utils.has_role(request.user, "expense paymaster"):
        expenses = expenses.filter(Q(user=request.user) | Q(user__in=user_team))

    expenseTable = ExpenseTable(expenses, orderable=True)
    RequestConfig(request, paginate={"per_page": 50}).configure(expenseTable)

    if "csv" in request.GET:
        return tableToCSV(expenseTable, filename="expenses.csv")

    return render(request, "expense/expense_archive.html",
                  {"expense_table": expenseTable,
                   "user": request.user})


@pydici_non_public
def mission_expenses(request, mission_id):
    """Page fragment that display expenses related to given mission"""
    try:
        mission = Mission.objects.get(id=mission_id)
        if mission.lead:
            expenses = Expense.objects.filter(lead=mission.lead).select_related().prefetch_related("clientbill_set")
        else:
            expenses = []
    except Mission.DoesNotExist:
        expenses = []
    return render(request, "expense/expense_list.html",
                  {"expenses": expenses,
                   "user": request.user})


@pydici_non_public
@pydici_feature("reports")
def chargeable_expenses(request):
    """Display all chargeable expenses that are not yet charged in a bill"""
    expenses = Expense.objects.filter(chargeable=True).select_related().prefetch_related("clientbill_set")
    expenses = [e for e in expenses if e.clientbill_set.all().count() == 0]
    return render(request, "expense/chargeable_expenses.html",
                  {"expenses": expenses,
                   "user": request.user})


@pydici_non_public
@pydici_feature("reports")
def update_expense_state(request, expense_id, transition_id):
    """Do workflow transition for that expense."""
    error = False
    message = ""

    try:
        expense = Expense.objects.get(id=expense_id)
        if expense.user == request.user and not utils.has_role(request.user, "expense administrator"):
            message =  _("You cannot manage your own expense !")
            error = True
    except Expense.DoesNotExist:
        message =  _("Expense %s does not exist" % expense_id)
        error = True

    if not error:
        try:
            transition = Transition.objects.get(id=transition_id)
        except Transition.DoesNotExist:
            message = ("Transition %s does not exist" % transition_id)
            error = True

        if wf.do_transition(expense, transition, request.user):
            message = _("Successfully update expense")

            # Prune expense in terminal state (no more transition) and without payment (ie paid ith corporate card)
            # Expense that need to be paid are pruned during payment process.
            if expense.corporate_card and wf.get_state(expense).transitions.count() == 0:
                expense.workflow_in_progress = False
                expense.save()
        else:
            message = _("You cannot do this transition")
            error = True

    response = {"message": message,
                "expense_id": expense_id,
                "error": error}

    return HttpResponse(json.dumps(response), content_type="application/json")


@pydici_non_public
@pydici_feature("management")
def expense_payments(request, expense_payment_id=None):
    readOnly = False
    if not request.user.groups.filter(name="expense_paymaster").exists() and not request.user.is_superuser:
        readOnly = True
    try:
        if expense_payment_id:
            expensePayment = ExpensePayment.objects.get(id=expense_payment_id)
    except ExpensePayment.DoesNotExist:
        messages.add_message(request, messages.ERROR, _("Expense payment %s does not exist" % expense_payment_id))
        expense_payment_id = None
        expensePayment = None

    if readOnly:
        expensesToPay = []
    else:
        expensesToPay = Expense.objects.filter(workflow_in_progress=True, corporate_card=False, expensePayment=None)
        expensesToPay = [expense for expense in expensesToPay if wf.get_state(expense).transitions.count() == 0]

    try:
        consultant = Consultant.objects.get(trigramme__iexact=request.user.username)
        user_team = consultant.userTeam()
    except Consultant.DoesNotExist:
        user_team = []

    expensePayments = ExpensePayment.objects.all()
    if not utils.has_role(request.user, "expense paymaster"):
        expensePayments = expensePayments.filter(Q(expense__user=request.user) | Q(expense__user__in=user_team)).distinct()

    if request.method == "POST":
        if readOnly:
            # A bad user is playing with urls...
            return HttpResponseRedirect(urlresolvers.reverse("forbiden"))
        form = ExpensePaymentForm(request.POST)
        if form.is_valid():
            if expense_payment_id:
                expensePayment = ExpensePayment.objects.get(id=expense_payment_id)
                expensePayment.payment_date = form.cleaned_data["payment_date"]
            else:
                expensePayment = ExpensePayment(payment_date=form.cleaned_data["payment_date"])
            expensePayment.save()
            for expense in Expense.objects.filter(expensePayment=expensePayment):
                expense.expensePayment = None  # Remove any previous association
                expense.save()
            if form.cleaned_data["expenses"]:
                for expense in form.cleaned_data["expenses"]:
                    expense.expensePayment = expensePayment
                    expense.workflow_in_progress = False
                    expense.save()
            return HttpResponseRedirect(urlresolvers.reverse("expense.views.expense_payments"))
        else:
            print "form is not valid"

    else:
        if expense_payment_id:
            expensePayment = ExpensePayment.objects.get(id=expense_payment_id)
            form = ExpensePaymentForm({"expenses": list(Expense.objects.filter(expensePayment=expensePayment).values_list("id", flat=True)), "payment_date": expensePayment.payment_date})  # A form that edit current expense payment
        else:
            form = ExpensePaymentForm(initial={"payment_date": date.today()})  # An unbound form

    return render(request, "expense/expense_payments.html",
                  {"modify_expense_payment": bool(expense_payment_id),
                   "expense_payment_table": ExpensePaymentTable(expensePayments),
                   "expense_to_pay_table": ExpenseTable(expensesToPay),
                   "read_only": readOnly,
                   "form": form,
                   "user": request.user})


@pydici_non_public
@pydici_feature("management")
def expense_payment_detail(request, expense_payment_id):
    """Display detail of this expense payment"""
    if not request.user.groups.filter(name="expense_requester").exists():
        return HttpResponseRedirect(urlresolvers.reverse("forbiden"))
    try:
        if expense_payment_id:
            expensePayment = ExpensePayment.objects.get(id=expense_payment_id)
        if not (expensePayment.user() == request.user or\
           utils.has_role(request.user, "expense paymaster") or\
           utils.has_role(request.user, "expense manager")):
            return HttpResponseRedirect(urlresolvers.reverse("forbiden"))

    except ExpensePayment.DoesNotExist:
        messages.add_message(request, messages.ERROR, _("Expense payment %s does not exist" % expense_payment_id))
        return redirect(expense_payments)

    return render(request, "expense/expense_payment_detail.html",
                  {"expense_payment": expensePayment,
                   "expense_table": ExpenseTable(expensePayment.expense_set.all()),
                   "user": request.user})
