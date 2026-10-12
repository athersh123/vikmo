"""URL Configuration for dealerdesk project."""
from django.contrib import admin
from django.urls import include, path, re_path
from core.views import not_found_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("core.urls")),
    path("api/", include("catalog.urls")),
    path("api/", include("orders.urls")),
    path("api/", include("supplier.urls")),
    re_path(r"^.*$", not_found_view),
]

handler404 = "core.exceptions.django_404_handler"
handler500 = "core.exceptions.django_500_handler"
