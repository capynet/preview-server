Servidor preview server: 65.108.243.53

Para el desarrollo en local, tener las cli de gitlab, github, cloudflare suele ser una buena idea. 

El encriptado del vault pass: **preview-mr**

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

# UI [ui](ui)
Is the UI of preview manager ([server](server))



# TODO
- en lugar de comprimir el dir files a lo mejor es buena idea usar un rzync con este layer especial que usa docker para solo mopdificar por encima (overlay layer o asi).
- Si decido seguir creando un tar me gustaria comprimir con multiples core.
- Cuando este todo estable hay que quitar el debug "set -x"
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
- Cambiar el almacenamiento de configuraciones de /var/www/preview-manager/app-config.json a DB.
- Un caso de uso que me gustaria tener cubierto: si alguien tiene una plataofrma custom como la de DXP de dropsolid, si quieren dar opciones de link, uli y demas, con el cli me basta verdad?
- Necesitaria poder cambiar configuraciones de ddev.algo como config.override.yml e incluso la podibilidad de modificar la configuracion por acada Mr (por si la v de php es nueva).
- IDEA que no se si va en este proyecto. "Live backups". Que me permita viajar hacia atras a una DB y commit en particular (el que haya estado desplegado el dia que se hizo el backup). Es super util cuando un cliente necesita ver algo del pasado como contenidos borrados.
- IDEA cuando un preview acaba necesitamos actualizar en gitlab la url
- idea: descviar los logs a dodne podas mos accederlos vusuaoment.e Tal we no herramientas titanicas pero si visualizadoews que permitab ver y  uu un comando cli que permitea estrimeo de los y un linx de deecarga.
- Habra que poder limitar el acceso en base al dominio (por empresa)
- necesito dar la posibilidad de definir la ssh key a un usuario para usarla a la hora de hacer ssh a un preview. quiero crear un nuevo comando en la cli que permita hacer "ssh" al contenedor web de una preview. si un usuario quiere suar ese comando y no tiene su ssh key podemos dalr un link a su cuentta y comentarle que primero tiene que poner su clave alli. Tengo entendido que no necesito ssh para acceder a los contenedores. 

Otro asunto para resolver:
   Problema
   
   Actualmente solo hay una DB base por proyecto. Se necesita poder tener previews con DBs distintas (ej: main con DB live, develop con DB sanitizada).
   
   Solución
   
   Nuevo flag --target en preview push db que importa la DB directamente en una preview existente, en vez de subirla como base file del proyecto.
   
   Flujo
   
   preview push db --target=branch-main
   
   
   1. CLI hace dump local (drush sql:dump, como ya hace)
      2. CLI sube el gzip a POST /api/previews/{project}/{preview_name}/db/import
      3. Backend recibe el gzip y ejecuta gunzip | docker exec -i {container}-db mysql -u drupal -pdrupal drupal
   
   Cambios necesarios
   
   CLI:
   - Flag --target=<preview_name> en push db
     - Si --target se pasa, enviar al endpoint de import en vez de al de base files
   
   Backend:
   - Nuevo endpoint POST /api/previews/{project}/{preview_name}/db/import
     - Recibe el gzip como body/multipart
     - Ejecuta el import en el container de DB de esa preview
     - Opcionalmente ejecuta drush cr después
   
   Ventajas
   
   - Sin config extra, sin variantes, sin aliases drush
     - Reutiliza el dump que el CLI ya sabe hacer
     - Cada preview puede tener la DB que quieras sin afectar las demás
     - Retrocompatible: preview push db sin --target sigue funcionando como antes

- Voy a necesitar que los drupal almacenen sus logs junto a los de apache, php etc en un lugar centralizado facil de revisar. elk o algo mas simple?
- Voy a necesitar ram, cpu y disco stats simples para tener un overview facil.