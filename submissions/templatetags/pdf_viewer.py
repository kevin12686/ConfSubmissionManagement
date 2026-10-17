from django import template
from django.urls import reverse

from submissions.services.pdf_viewer import pdf_viewer_url


register = template.Library()
register.filter("pdf_viewer_url", pdf_viewer_url)


@register.simple_tag
def pdf_viewer(route, *args):
    return pdf_viewer_url(reverse(route, args=args))
