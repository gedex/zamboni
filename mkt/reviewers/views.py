import datetime

from django import http
from django.conf import settings
from django.db.models import Q
from django.shortcuts import redirect

import jingo
from tower import ugettext as _

from access import acl
import amo
from amo import messages
from amo.utils import urlparams
from addons.decorators import addon_view
from addons.models import Version
from amo.decorators import permission_required
from amo.urlresolvers import reverse
from amo.utils import paginate
from editors.forms import MOTDForm
from editors.models import EditorSubscription
from editors.views import reviewer_required
from mkt.developers.models import ActivityLog
from mkt.webapps.models import Webapp
from reviews.models import Review
from zadmin.models import get_config, set_config

from . import forms
from .models import AppCannedResponse, EscalationQueue, RereviewQueue


QUEUE_PER_PAGE = 100


@reviewer_required
def home(request):
    durations = (('new', _('New Apps (Under 5 days)')),
                 ('med', _('Passable (5 to 10 days)')),
                 ('old', _('Overdue (Over 10 days)')))

    progress, percentage = _progress()

    data = context(
        reviews_total=ActivityLog.objects.total_reviews(webapp=True)[:5],
        reviews_monthly=ActivityLog.objects.monthly_reviews(webapp=True)[:5],
        #new_editors=EventLog.new_editors(),  # Bug 747035
        #eventlog=ActivityLog.objects.editor_events()[:6],  # Bug 746755
        progress=progress,
        percentage=percentage,
        durations=durations
    )
    return jingo.render(request, 'reviewers/home.html', data)


def queue_counts(type=None, **kw):
    counts = {
        'pending': Webapp.objects.pending().count(),
        'rereview': RereviewQueue.objects.count(),
        'escalated': EscalationQueue.objects.count(),
    }
    rv = {}
    if isinstance(type, basestring):
        return counts[type]
    for k, v in counts.items():
        if not isinstance(type, list) or k in type:
            rv[k] = v
    return rv


def _progress():
    """Returns unreviewed apps progress.

    Return the number of apps still unreviewed for a given period of time and
    the percentage.
    """

    days_ago = lambda n: datetime.datetime.now() - datetime.timedelta(days=n)
    qs = Webapp.objects.pending()
    progress = {
        'new': qs.filter(created__gt=days_ago(5)).count(),
        'med': qs.filter(created__range=(days_ago(10), days_ago(5))).count(),
        'old': qs.filter(created__lt=days_ago(10)).count(),
        'week': qs.filter(created__gte=days_ago(7)).count(),
    }

    # Return the percent of (p)rogress out of (t)otal.
    pct = lambda p, t: (p / float(t)) * 100 if p > 0 else 0

    percentage = {}
    total = progress['new'] + progress['med'] + progress['old']
    percentage = {}
    for duration in ('new', 'med', 'old'):
        percentage[duration] = pct(progress[duration], total)

    return (progress, percentage)


def context(**kw):
    ctx = dict(motd=get_config('mkt_reviewers_motd'),
               queue_counts=queue_counts())
    ctx.update(kw)
    return ctx


def _review(request, addon):
    version = addon.latest_version

    if (not settings.DEBUG and
        addon.authors.filter(user=request.user).exists()):
        messages.warning(request, _('Self-reviews are not allowed.'))
        return redirect(reverse('reviewers.home'))

    tab = request.GET.get('tab', 'pending')
    form = forms.get_review_form(request.POST or None, request=request,
                                 addon=addon, version=version, queue=tab)

    redirect_url = reverse('reviewers.apps.queue_%s' % tab)

    num = request.GET.get('num')
    paging = {}
    if num:
        try:
            num = int(num)
        except (ValueError, TypeError):
            raise http.Http404
        total = queue_counts(tab)
        paging = {'current': num, 'total': total,
                  'prev': num > 1, 'next': num < total,
                  'prev_url': urlparams(redirect_url, num=num - 1, tab=tab),
                  'next_url': urlparams(redirect_url, num=num + 1, tab=tab)}

    is_admin = acl.action_allowed(request, 'Addons', 'Edit')

    if request.method == 'POST' and form.is_valid():
        form.helper.process()
        if form.cleaned_data.get('notify'):
            EditorSubscription.objects.get_or_create(user=request.amo_user,
                                                     addon=addon)
        if form.cleaned_data.get('adminflag') and is_admin:
            addon.update(admin_review=False)
        messages.success(request, _('Review successfully processed.'))
        return redirect(redirect_url)

    canned = AppCannedResponse.objects.all()
    actions = form.helper.actions.items()

    statuses = [amo.STATUS_PUBLIC, amo.STATUS_LITE,
                amo.STATUS_LITE_AND_NOMINATED]

    try:
        show_diff = (addon.versions.exclude(id=version.id)
                                   .filter(files__isnull=False,
                                       created__lt=version.created,
                                       files__status__in=statuses)
                                   .latest())
    except Version.DoesNotExist:
        show_diff = None

    # The actions we should show a minimal form from.
    actions_minimal = [k for (k, a) in actions if not a.get('minimal')]

    # We only allow the user to check/uncheck files for "pending"
    allow_unchecking_files = form.helper.review_type == "pending"

    versions = (Version.objects.filter(addon=addon)
                               .exclude(files__status=amo.STATUS_BETA)
                               .order_by('-created')
                               .transform(Version.transformer_activity)
                               .transform(Version.transformer))

    pager = paginate(request, versions, 10)

    num_pages = pager.paginator.num_pages
    count = pager.paginator.count

    ctx = context(version=version, product=addon, pager=pager,
                  num_pages=num_pages, count=count,
                  flags=Review.objects.filter(addon=addon, flag=True),
                  form=form, paging=paging, canned=canned, is_admin=is_admin,
                  status_types=amo.STATUS_CHOICES, show_diff=show_diff,
                  allow_unchecking_files=allow_unchecking_files,
                  actions=actions, actions_minimal=actions_minimal,
                  tab=tab)

    return jingo.render(request, 'reviewers/review.html', ctx)


@permission_required('Apps', 'Review')
@addon_view
def app_review(request, addon):
    return _review(request, addon)


def _queue(request, qs, tab, pager_processor=None):
    review_num = request.GET.get('num')
    if review_num:
        try:
            review_num = int(review_num)
        except ValueError:
            pass
        else:
            try:
                # Force a limit query for efficiency:
                start = review_num - 1
                row = qs[start:start + 1][0]
                # Get the addon if the instance is one of the *Queue models.
                if not isinstance(row, Webapp):
                    row = row.addon
                return redirect(urlparams(
                    reverse('reviewers.apps.review', args=[row.app_slug]),
                    num=review_num, tab=tab))
            except IndexError:
                pass

    per_page = request.GET.get('per_page', QUEUE_PER_PAGE)
    pager = paginate(request, qs, per_page)

    if pager_processor:
        addons = pager_processor(pager)
    else:
        addons = pager.object_list

    return jingo.render(request, 'reviewers/queue.html', context(**{
        'addons': addons,
        'pager': pager,
        'tab': tab,
    }))


@permission_required('Apps', 'Review')
def queue_apps(request):
    qs = (Webapp.objects.pending().filter(disabled_by_user=False)
                        .order_by('created'))
    return _queue(request, qs, 'pending')


@permission_required('Apps', 'Review')
def queue_rereview(request):
    qs = (RereviewQueue.objects.filter(addon__disabled_by_user=False)
                        .order_by('created'))
    return _queue(request, qs, 'rereview',
                  lambda p: [r.addon for r in p.object_list])


@permission_required('Apps', 'Review')
def queue_escalated(request):
    qs = (EscalationQueue.objects.filter(addon__disabled_by_user=False)
                         .order_by('created'))
    return _queue(request, qs, 'escalated',
                  lambda p: [r.addon for r in p.object_list])


@permission_required('Apps', 'Review')
def logs(request):
    data = request.GET.copy()

    if not data.get('start') and not data.get('end'):
        today = datetime.date.today()
        data['start'] = datetime.date(today.year, today.month, 1)

    form = forms.ReviewAppLogForm(data)

    approvals = ActivityLog.objects.review_queue(webapp=True)

    if form.is_valid():
        data = form.cleaned_data
        if data.get('start'):
            approvals = approvals.filter(created__gte=data['start'])
        if data.get('end'):
            approvals = approvals.filter(created__lt=data['end'])
        if data.get('search'):
            term = data['search']
            approvals = approvals.filter(
                    Q(commentlog__comments__icontains=term) |
                    Q(applog__addon__name__localized_string__icontains=term) |
                    Q(applog__addon__app_slug__icontains=term) |
                    Q(user__display_name__icontains=term) |
                    Q(user__username__icontains=term)).distinct()

    pager = amo.utils.paginate(request, approvals, 50)
    data = context(form=form, pager=pager, ACTION_DICT=amo.LOG_BY_ID)
    return jingo.render(request, 'reviewers/logs.html', data)


@reviewer_required
def motd(request):
    form = None
    motd = get_config('mkt_reviewers_motd')
    if acl.action_allowed(request, 'AppReviewerMOTD', 'Edit'):
        form = MOTDForm(request.POST or None, initial={'motd': motd})
    if form and request.method == 'POST' and form.is_valid():
            set_config(u'mkt_reviewers_motd', form.cleaned_data['motd'])
            return redirect(reverse('reviewers.apps.motd'))
    data = context(form=form)
    return jingo.render(request, 'reviewers/motd.html', data)
