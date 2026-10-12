"""URL routes for Forecasting."""
from django.urls import path
from forecasting.views import ReorderSuggestionsView, SkuForecastView

urlpatterns = [
    path("forecast/<str:sku>/", SkuForecastView.as_view(), name="sku-forecast"),
    path("reorder-suggestions/", ReorderSuggestionsView.as_view(), name="reorder-suggestions"),
]
