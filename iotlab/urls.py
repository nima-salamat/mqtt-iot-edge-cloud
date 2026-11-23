from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from dashboard import views as dashboard_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("api/", include("dashboard.api_urls")),
    path("health/", dashboard_views.health, name="health"),
    path("", include(("dashboard.urls", "dashboard"), namespace="dashboard")),
]
