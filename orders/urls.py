"""URLs for Orders endpoints."""
from django.urls import path
from orders import views

urlpatterns = [
    path("orders/", views.OrderListCreateView.as_view(), name="order-list-create"),
    path("orders/<str:order_number>/", views.OrderDetailUpdateView.as_view(), name="order-detail-update"),
    path("orders/<str:order_number>/confirm/", views.OrderConfirmView.as_view(), name="order-confirm"),
    path("orders/<str:order_number>/dispatch/", views.OrderDispatchView.as_view(), name="order-dispatch"),
    path("orders/<str:order_number>/deliver/", views.OrderDeliverView.as_view(), name="order-deliver"),
    path("orders/<str:order_number>/cancel/", views.OrderCancelView.as_view(), name="order-cancel"),
]
