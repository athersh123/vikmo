"""URLs for Supplier synchronization API."""
from django.urls import path
from supplier import views

urlpatterns = [
    path("sync/supplier/", views.SupplierSyncView.as_view(), name="supplier-sync"),
]
