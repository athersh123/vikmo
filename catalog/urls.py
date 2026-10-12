"""URLs for Catalog and Inventory endpoints."""
from django.urls import path
from catalog import views

urlpatterns = [
    path("products/", views.ProductListCreateView.as_view(), name="product-list-create"),
    path("products/<str:sku>/", views.ProductDetailView.as_view(), name="product-detail"),
    path("inventory/<str:sku>/", views.InventoryUpdateView.as_view(), name="inventory-update"),
    path("dealers/", views.DealerListCreateView.as_view(), name="dealer-list-create"),
    path("dealers/<int:pk>/", views.DealerDetailView.as_view(), name="dealer-detail"),
]
