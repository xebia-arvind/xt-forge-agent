from django.contrib import admin
from import_export.admin import ImportExportModelAdmin
from .models import Clients, UserClient
# Register your models here.

class ClientAdmin(ImportExportModelAdmin):
    list_filter = ('status','clientname')
    list_display = ('secret_key','logo_preview','clientname','created_on')
    
@admin.register(UserClient)
class UserClientAdmin(admin.ModelAdmin):
    filter_horizontal = ("clients",)  
    list_display = ('user','client_list') 

    def client_list(self, obj):
        return ", ".join(
            obj.clients.values_list("clientname", flat=True)
        )

    client_list.short_description = "Clients"
    

admin.site.register(Clients, ClientAdmin)

