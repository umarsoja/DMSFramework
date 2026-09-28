from django.http import HttpResponse
from django.shortcuts import render


def landing(request):
    """Public presentation page; no database or authentication dependency."""
    return render(request, "pages/landing.html")

def health_check(request):
    return HttpResponse("TradeFlow is running successfully.")
