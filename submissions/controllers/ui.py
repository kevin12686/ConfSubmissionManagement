from django.shortcuts import render

from submissions.services.workflow_alerts import workflow_alert_counts


def workflow_alerts(request):
    return render(
        request,
        "submissions/partials/workflow_alerts.html",
        {"global_workflow_alerts": workflow_alert_counts()},
    )
