"""dj_hetmech URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/2.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.urls import path, include
from rest_framework import routers
from dj_hetmech_app import views

router = routers.DefaultRouter()
router.register("nodes", views.NodeViewSet, basename="node")

urlpatterns = [
    path('v1/', views.api_root),
    path('v1/', include(router.urls)),
    path('v1/querymetapath/', views.QueryMetapathView.as_view(), name="query-metapath"),
    path('v1/querypath/', views.QueryPathView.as_view(), name="query-path"),
]
