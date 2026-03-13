Servidor preview server: 91.99.157.66

Para el desarrollo en local, tener las cli de gitlab, github, cloudflare suele ser una buena idea. 

El encriptado del vault pass: **preview-mr**
Para verlo:
ansible-vault view inventory/group_vars/all/vault.yml

Para editarlo:
ansible-vault edit inventory/group_vars/all/vault.yml


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
- Necesito que los mr aborten el build actual cuando llega un nuevo hook. 
- Necesito un boton para abortar el build actual. Es util por ejemplo si necesito añadir una env var. en lugar de esperar a que acabe puedo abortarlo y lanzarlo.
- preview push files me obliga a iniciar sesion y a seleccionar una organizacion. necesito que el tema de las orgnaizaciones sea mas transparente para no tener que seleccionar la orgnizacion. 
- Para pulir el deploy necesito revisar los flows principales: creacion y gestion de usuarios, roles, accesos a sus proyecos y organizaciones. asegurarme que no se pueden acceder proyectos y organizaciones desde otrras cuentas. que el proceso de creacion de previews funciona y que se usan todas las herramientas que tenemos (queues). Que se pueden crear y borrar en secuencia previes sin que colapse el servicio
- 2fa necesario sobre todo para mi que soy superadmin y en realidad para cualquiera porque las DB son tema sensible.
- NEcesito tener tier free y paid one. el free permite crear una sola preview por poryecto sin limites en nada mas. (ampliar mas la idea)  
- Drush aliases para los preview para poder lanzar comando "preview drush"
- Extraer toda la configuracion sensible (claves token etc) a lugar seguro y configurable para poder actualizarlos en el futuro sin tanto ptoblema. 
- Voy a necesitar alguna sanitizacion para las db subidas o de eso se hace cargo el desarrolladdor?
- creo que esto ya existe: Poder especificar un proceso de despliegue personalizado por rama! (Super útil cuando estás creando algo nuevo que requiera una configuración específica). Probablemente necesite ser especificado desde la conf de la rama ya que no se puede andar coniteando cambios para hacer pruebas.
- Acceso por consola via ui y posibilidad de configurar pu key para acceder a los ddev? (creo que no porque se puede acceder sin mas)
- IDEA cuando un preview acabe de generarse necesitamos proporcionar la url del preview en el MR de gitlab.
- 
- Cuando este todo estable hay que quitar el debug "set -x"
- soporte multisite.
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

- Un caso de uso que me gustaria tener cubierto: si alguien tiene una plataofrma custom como la de DXP de dropsolid, si quieren dar opciones de link, uli y demas, con el cli me basta verdad? (hablo a nivel de integracion)
- idea: descviar los logs a dodne podas mos accederlos vusuaoment.e Tal we no herramientas titanicas pero si visualizadoews que permitab ver y  uu un comando cli que permitea estrimeo de los y un linx de deecarga.
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
- ssh y stats.
necesito mailpit pero tambine una config por ui quepermita desactivarlo por cada preview (hay casos en lls que los email necesitan ser cofirmados). 
- . Soporte Drupal decoupled (container Node.js adicional) — para competir con Upsun en ese nicho  
- el cache estilo varnish que tiene el servidor web que reemplaza a apache en los preview se puede activar y desactivar? hace falta algun modulo en drupal? (so voy para adelante con esto deberia poder activarlo y desactivarlo desde la ui mas que desde drupal)
- - Si quiero qeu esto funcione. Los usuarios freelance deberiantener esto gratis o por lo menos la opcion de hostearselo ellos mismos o un tier que sea 0€
- Voy a neceitar algun check que evite que el servidor colapse si el espacio en disco se acaba.
- Deebria haber algun script que vaya reocrriendo en busca de previews huerfanas?
- preview autocompletion deberia hacerse automaticamente en lugar de necesitar lanzar el comando a mano. 
- Es posible permirir el uso de apache o OpenLiteSpeed indistintamente?
- Alternativamente a varnish tenemos (LiteSpeed Cache) con algo de integracion en drupal. no se si hay un modulo sino hay que hacer uno.
- Deberia automatizar el deploy de ansible o la generacion de version del cli cuando pusheo a master?
- Agregar cron a los preview. 
- En el modal "New Preview from Branch" quiero que las ramas esten listadas de mas nueva a mas vieja. 
- Usar github actions para compilar el cli, la ui
- en la visualizacion de uso del cpu me gustaria saber el indice de carga en los ultimos mins
- pregunta: se podria tener un snapshot de una imagen de docker preparada para ser reutilizada en segundos? por ejemplo el docker de la db siempre es el mismo en cada rebuild hasta que se     
  carga una nueva db. RESPUESTA:
  -  Sí, hay varias opciones. Para el caso de la DB:

    1. Imagen pre-cargada con docker commit                                                                                                                                                       
       Después de importar la base DB, hacer docker commit del container MySQL como imagen custom (preview-db-soudal:latest). Los nuevos previews usan esa imagen y arrancan con los datos ya
       cargados. Problema: MySQL guarda datos en volumen, no en la capa del container, así que habría que ajustar eso.

    2. Custom image con datos baked-in (más robusto)
       Crear un Dockerfile que arranca MySQL, importa el dump, y guarda el datadir dentro de la imagen. Cada vez que se sube un nuevo base DB, se rebuilda la imagen. Los previews arrancan en
       segundos con datos listos.

    3. Volume snapshots (más rápido, más complejo)
       Con ZFS o btrfs podés hacer snapshots instantáneos del volumen de datos. Clonar un volumen de 500MB toma milisegundos. Pero requiere que el servidor use esos filesystems.

  La opción más práctica para tu caso sería la 2: cuando alguien sube un nuevo base DB via preview push db, el backend:
    1. Levanta un container MySQL temporal
    2. Importa el dump
    3. Hace docker commit → preview-db:{project}:latest
    4. Los nuevos previews usan esa imagen en vez de mysql:8.0 + import
- Deberia dar algun soporte para MCP para poder conectar con las previews desde local
- voy a necesitar poder especificar en preview push db/files un db o dir files arbitrario y un .sql o .sql.hz o files.tgz o tar.gz 
- Cuando dse crea el cache de la db se hace a la hora de crear el primer preview. Me pregunto si es posible sacar ese caso a un proceso en backgrround para no bloquear la generacion del preview. Y si no es posible por lo menos dar un poco mas de info "creando cache de esta db apras er usadaen las siguientes preview." ademas si se puede informar el progreso mejor que mejor.
- El backend ya tiene los endpoints (/api/config/cloud-resources y /api/config/cloud-costs), pero falta la UI en el frontend para mostrarlos
- viste que tengo estadisticas de cpu ram y disco en el server principal? podriamos suar ese widget dentro de los detalles de cada preview para ver los recursos usados en cada uno?  
- cada tenant u organizacion podria configurar sus propios dominios? la url de preview seguiria siendo funcionar. lo del dominio custom seria añadirlo encima. Ojo. solo puedo permitir subdominios de su dominio registrado en la organizacion. A lo sumo puedo permitir subdominios de un sitio alternativo pero nunca dominuios base apra que no lo usen como pagina web. 
- Necesito en los scrip de deploy un step mas que se ejecute despues de los script de deploy normales. cuando un deploy acaba (yq ueda en verde y accesible) recien ahi se lanza el script post deploy que se puede usar para indexar contenidos o tareas que son muy pesadas ocmo cron run o cosas asi.
- necesito integrar ia en lugares donde tenga sentido: "activa los proyectos que mas actividad tienen", "Si vas viendo previews que ninca se visitan despues de ser creadas en un proyeco en particular mejor no crees previews e informa al usuario que no se estan creando previews por falta de uso". Permite dejar un proment de lo que se espera a la hrtoa de hacer un erase automatico como por ejenmplo "en este proyecto por lo general revisamos las preview en un dia concreto aSI QUE NO TIENE SENTIDO QUE LAS CREES AUTOMATICAMENTE. MEJOR CREALAS CUANDO CREE UN mr A MASTER QUE SE LLAME "rELEASE XXX""""
- en el detalle de un preview el widget de uso de recursos de la vm aparece y desaparece cuando esta transiocnando de estaods o reiniciando. MNo hay drama con eso pero en lugar de ocultarlo a lo mejor podemos simplemente dejarlo grisado.
- Quiero poder marcar como favoritos algunso preview de rama o mr. Cuando se borre el preview esta desaparece silencionamente.
- Necesito un black list para deshabilitar ramas que no tiene sentido que creen mr previews todo el tiempo (rama dev o master). Se pueden añadir y quitar y el listado se limpia solito como el listado de favoritos.
- estaria bien ver en el listado de preview o en su detalle cual es el destino de un MR (a master u otra rama). Includo esto me da la idea de agrupar por destinations onda arbol. 