from django import template
from accounts.decorators import _has_permission

register = template.Library()


@register.simple_tag
def has_perm(user, permission_code):
    """{% has_perm user 'shipment.upload' as can_upload %}"""
    return _has_permission(user, permission_code)
