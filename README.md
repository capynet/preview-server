Servidor gitlab runner: 89.167.20.45
Servidor preview server: 65.108.243.53

tener las cli de gitlab, github, cloudflare suele ser una buena idea. 


A tener en cuenta: La clave ssh para ocmunicar entre servidores se genera en uno y se envia al otro doto por ansible.
## Server [server](server)

This dir contains the server app itself
```
/www/previews/server/preview-manager
```

#### Deployed previews at

```
/var/www/previews/
```

#### Resources (base db and files)

```
/backups
```

# Drupal side [drupal-configs](drupal-configs)
There are 3 key parts there:

`ddev push-to-preview-server` is for generating the base files and DB for the previews server. It pushes them
`.gitlab-ci.yml` && `scripts/deploy-preview.sh` that tells to gitlab to deploy the preview and the script itself is the one thar really does the deploy. It is done there (for now) because if i want to do this from the previews server manager it does not show output on gitlab deploy pipe.

Also you have
```php
 // Load preview settings only on the preview server
 if (getenv('IS_PREVIEW_SERVER') === 'true') {
   if (file_exists($app_root . '/' . $site_path . '/settings.preview.php')) {
     include $app_root . '/' . $site_path . '/settings.preview.php';
   }
 }
```

settings.preview.php is a copy of [settings.ddev.php](../drupal-test-2/web/sites/default/settings.ddev.php) customized for the preview server. only loaded there.


Importart to configure manually on gitlab:
1. Ve a tu proyecto GitLab
2. CI/CD > Schedules
3. New schedule
4. Configuración:
   - Description: Cleanup closed/merged MRs
   - Interval: */5 * * * * (cada 5 minutos) o */1 * * * * (cada minuto si tienes Premium)
   - Target branch: main
   - Active: ✅
5. Save


# UI [ui](ui)
Is the UI of preview manager ([server](server))



# TODO
- en lugar de comprimir el dir files a lo mejor es buena idea usar un rzync con este layer especial que usa docker para solo mopdificar por encima (overlay layer o asi).
- Si decido seguir creando un tar me gustaria comprimir con multiples core.
- Los preview solo deberian ser accesibles si estas logueado con una cuenta de gmail valida
- Cuando haga lo documentacion mencionar que en el MR se crea una url para ver el environment creado !!!!
- Cuando este todo estable hay que quitar el debug "set -x"
- Todavia hay archivos en el preview manager que tienen "simple". Esto es porque en un punto decidi simplificar la solucion pero con el tiempo se convirtio en la unica solucion por lo que llamar "simple" a nivel archivos y codigo ya no tiene sentido.
- acceso a la plataforma y comunicacion con el backend solo si esta logueado.
- Roles admin y developer (para acceder pero no modificar) y manager que puede modificar configuraciones a nivel proyecto.
- Necesito separar la logica de db y files de [deploy-preview.sh](../drupal-test-2/scripts/ci/deploy-preview.sh) porque esto es algo que deberia hacer el server de previews y no dejarlo en las manos del desarrollador. Este archivo solo deberia permitir lanzar acciones dentro del conteneidor como un drush deploy, cr, etc.
- Revisar cuidadosamente cada proceso. Necesito asegurarme que todo se ejecuta en el orden necesario y llamando a los scripts que yo creo .

## Futuras mejoras
- El cron para limpieza en lugar de ser configurado desde gitlab que sea gestionado por el preview manager
- push-to-preview-server toma la db y files de la instalacion local y tiene harcodeados varios valores como el server o el project name. Idealmente me gustaria que esto pueda ser gestionado desde la ui. configurar un env desde el cual tomar la info usando drush sync o poder especificar una ruta en un server de backups del cual tomar los backup ya generados (ya seria la bomba listar los posibles backups y tener una opcion "tomar el mas reciente").
- Como cada proyecto puede llegar a ser muy pesado, idealmente me gustaria tener algo como el preview manager en un servidor y los gitlab runners en servidores individuales conectados en una red interna cosa que el preview manager solo coordine los gitlab runner que a su vez tendrian el runner y los sitios ddev correspondientes solo a su proyecto.
- soporte multisite.
- Posibilidad de descargar la db
- Acceso por consola via ui y posibilidad de configurar pu key para acceder a los ddev? (creo que no porque se puede acceder sin mas)
- posibilidad de definir variables de entorno exclusivas para el preview. va a haber veces que se este trabajando en una modificacion especifica que necesite sobreescribir o crear nuevas env vars.
- separar el gitlab runner del server de previews usando alguna arquitectura que me permita usar github action ademas.
- Proteger las URL con autj.js
- Poder especificar un proceso de despliegue personalizado por rama! (Super útil cuando estás creando algo nuevo que requiera una configuración específica). Probablemente necesite ser especificado desde la conf de la rama ya que no se puede andar coniteando cambios para hacer pruebas.
- Necesito una página para listar proyectos y la posibilidad de cambiarlo desde el header
- añadir la posibilidad de sobreescribir la configuración de Ddev config.yml onda config.preview.yml
- personalizar la URL  para cada proyecto o cada cliente?
- Agregar la posibilidad de ejecutar un script de despliegue automático o personalizar un script de despliegue específico para la rama actual o para el proyecto en general. Permitir vars configuración vía ui a nivel proyecto
- Si hay multisites necesito darles soporte
- NEcesito una cli en go que permita descargar la db y lanzar drush uli, restart, rebuild, stop, start, download db, download files (posibilidad de hacer esto de forma masiva a nivel proyecto tambien)
- Soporte para env vars a nivel proyecto y MR.
- Necesito que las imagenes hibernen cuando el tiempo congihurado a nivel proyecto o preview se cumpla
- necesito extraer los tokens de clousflare, gitlab runner y el certificado ssh.
- Las cookies tiene harcodeado domain=".preview-mr.com" y vamos a tenr que hacerlo generico o configurable para que funcione bien.
- La url https://api.preview-mr.com/ no deberia informar sobre lso endpoint. De hecho habria que revisar los endpoint y asegurarse que no queda nada expuesto que pueda ser peligroso.
- Para guiar al usuario hay documentar como conectar gitlab a la app de previews:
   Ir a https://gitlab.com/oauth/applications
   Añadir aplicacion
   
   "User login"
   Callback: https://api.preview-mr.com/api/gitlab/auth/callback
   Scopes: read_user
   
   
   Luego crear una nueva app para conectar la api:
   "Previews API"
   Callback: https://api.preview-mr.com/api/gitlab/connect/callback
   Scopes: api
   
   
   
   "User login"
   Application ID: 3a4c9a8e1626f825734902c265eda47787b56f1724f09d65419e88991ab228d4
   Secret: gloas-85522b0b96f9a61bf169e10231a2422a6c59325dc324de1535cb0d4aecf8e0ee
   Callback: https://api.preview-mr.com/api/gitlab/auth/callback
   Scopes: read_user
   
   "Previews API"
   Application ID: b05e4ef0609f8e02eec1bbe36770d1c847a155da9176b80bc568ba4b87dfe7e4
   Secret: gloas-4361304ca401bddf62cddac1cc37b3062b9c8e981fdada089765f44836d1acc6
   Callback: https://api.preview-mr.com/api/gitlab/connect/callback
   Scopes: api