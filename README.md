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

#### Previews

Cada preview corre en su propia VM (Hetzner Cloud). El código se clona y ejecuta directamente en la VM mediante el VM Agent.

#### Resources (base db and files)

Almacenados en Hetzner Object Storage (S3).

# UI [ui](ui)
Is the UI of preview manager ([server](server))



# TODO antes de prod. 
- Esto va a los casos de uso para publicitar: Un caso de uso que me parece genial para ejemplificar el uso de preview en ramas es si hay dos diseños o funcinaildades distintas para un mismo asunto y hace falt decidir cual nos gusta mas. Al mismo tiempo se puede tener un preview de cada uno de ellos.
- revisa la documentacion y añadir la documentacion de como configurar gitlab.
- Es posible estar asignado a organizaciones de otros owner? por ejemplo marcelo.tosco.diodati@gmail.com parte de Druploy y Dropsolid ademas de ser owner de su propio org?
- Post deploy sigue mostrando el tag "Deploying" aunque haya temrinado el deploy principal
# TODO
- A veces queres mandar un preview a druploy incluso si las regla required_jobs no pasa. Estariab bueno poder marcar a nivel preview la desactivacion (o a nivel proyecto por lo menos podes espefificar que ramas o MR se bypasean asi se logra crear la primer preview) 
- los preview estaria bueno que muestren la fecha de creacion de la VM, ultima actualizacion y tiempo rstante hasta que se elimine si no esta bloqueada.
- Posibilidad de configurar un proyecot para que use VM mas grandes. Es necesario por si algun proyecto se considera muy pesado. 
- Me gustaria poder ver si un cron esta corriendo. 
- Cuando un mr se mergea o cierra el comentario con la info de druploy puede ser eliminada. 
- Necesito mejorar los ocmentarios que se hacen en los MR. A veces por accidente se crea uno nuevo. Necesito tal vez añadir una comprobacion extra: a nivel hash (el que se usa en la url de los preview). buscar su precencia. A lo mejor ya esta hecho. la cuestion es que necesito evitar que acabe habiendo mas de un comentario en un MR.
- Permitir que el auto erase sea a nivel horas en lugar de dias. 
- arreglar la posibilidad de loguearme con la cuenta de gitlab.
- Si un MR no contiene cambios a nivel de gitlab no se hace nada. simplemente me informa que no hay cambios. En ese tipo de MR deberiamos hacer lo mismo que se hace con los mr en dranf: nada.
- Srupal tiene un scon que se podria configurar automaticamente como una configuracion inicial del proyecto. 
- Hay logs dentor del container que estaria bien poder exponer mediante "preview logs (todos), preview logs cron" etc o incluso podes hacer preview logs pull para descargarlos localmente y rpocesarlos. 
- Los env var no se pueden deshabilitar. deberia poder estar activo/inactivo por si en algun preview por ejemplo no la quiero porque tengo que hacer pruebas que necesitan ese valor ausente. 
- Cuando un MR se cierre estaria bien borrar el comentario que hay de druplpy. 
- Reemplazar PAT por oauth en la conexion de gitlab?
- Cuando abrimos ssh a un preview (drush ssh) me gustaria tener una consola con colores.
- El cli deberia servirse desde algun lugar estatico. Como empiecen a descargarlo de forma masica me va a colapsar el server
- Gitlab tiene un nuevo tipo de token granular. hay que darle soporte. 
- Necesito en la UI alguna forma de recibir feedback. Algun popup que permita rapidamente attachear un video o imagen y algun comentario. Es para que los usuarios puedan decirme qwue necesita ser mejorado o arreglado. 
- Necesito que el cli tenga autoreport de errores. Antes de usarlo imagino que primero vamos a tener que agregar un mensaje que el usuario confirme permitiendonos enviar informacion anonimizada para mejorar la herramienta. Una vez que lo haya aprobado, si algun comando falla o crashea deberia enviarse informacion util que nos permita arreglar el error para el futuro. 
- cada tenant u organizacion podria configurar sus propios dominios? la url de preview seguiria siendo funcionar. lo del dominio custom seria añadirlo encima. Ojo. solo puedo permitir subdominios de su dominio registrado en la organizacion. A lo sumo puedo permitir subdominios de un sitio alternativo pero nunca dominuios base apra que no lo usen como pagina web.
- El backend ya tiene los endpoints (/api/config/cloud-resources y /api/config/cloud-costs), pero falta la UI en el frontend para mostrarlos
- Deebria haber algun script que vaya reocrriendo en busca de previews huerfanas?
- Voy a neceitar algun check que evite que el servidor colapse si el espacio en disco se acaba.
- El dia uqe salgamos a prod necesito desactivar las herrmaientas de desarrollo de next. productionBrowserSourceMaps
- Necesito estadisticas de cuantos preview se crean en un proyecto, cuantos rebuild y updates. Cualquier info que me permita hacer estadisticas de uso.
- NEcesito poder correr toda la solucion en un servidor alternativo para desarrollar las nuevas funcionalidades. O por lo mejos poder correrlo integramente en local. 
- Necesito que los mr aborten el build actual cuando llega un nuevo hook. 
- Necesito un boton para abortar el build actual. Es util por ejemplo si necesito añadir una env var. en lugar de esperar a que acabe puedo abortarlo y lanzarlo.
- 2fa necesario sobre todo para mi que soy superadmin y en realidad para cualquiera porque las DB son tema sensible.
- NEcesito tener tier free y paid one. el free permite crear una sola preview por poryecto sin limites en nada mas. (ampliar mas la idea)  
- Extraer toda la configuracion sensible (claves token etc) a lugar seguro y configurable para poder actualizarlos en el futuro sin tanto ptoblema. 
- Voy a necesitar alguna sanitizacion para las db subidas o de eso se hace cargo el desarrolladdor?
- creo que esto ya existe: Poder especificar un proceso de despliegue personalizado por rama! (Super útil cuando estás creando algo nuevo que requiera una configuración específica). Probablemente necesite ser especificado desde la conf de la rama ya que no se puede andar coniteando cambios para hacer pruebas.
- Cuando este todo estable hay que quitar el debug "set -x"
- soporte multisite. ya esta implementado pero soudal es un bicho raro. Necesito un multisite real.
- Un caso de uso que me gustaria tener cubierto: si alguien tiene una plataofrma custom como la de DXP de dropsolid, si quieren dar opciones de link, uli y demas, con el cli me basta verdad? (hablo a nivel de integracion)
- idea: descviar los logs a dodne podas mos accederlos vusuaoment.e Tal we no herramientas titanicas pero si visualizadoews que permitab ver y  uu un comando cli que permitea estrimeo de los y un linx de deecarga.
- Voy a necesitar que los drupal almacenen sus logs junto a los de apache, php etc en un lugar centralizado facil de revisar. elk o algo mas simple?
- Voy a necesitar ram, cpu y disco stats simples para tener un overview facil.
- - necesito mailpit pero tambine una config por ui quepermita desactivarlo por cada preview (hay casos en lls que los email necesitan ser cofirmados).
- Soporte Drupal decoupled (container Node.js adicional) — para competir con Upsun en ese nicho
- Si quiero qeu esto funcione. Los usuarios freelance deberiantener esto gratis o por lo menos la opcion de hostearselo ellos mismos o un tier que sea 0€
- preview autocompletion deberia hacerse automaticamente en lugar de necesitar lanzar el comando a mano.
- Es posible permirir el uso de apache o OpenLiteSpeed indistintamente?
- Alternativamente a varnish tenemos (LiteSpeed Cache) con algo de integracion en drupal. no se si hay un modulo sino hay que hacer uno.
- En el modal "New Preview from Branch" quiero que las ramas esten listadas de mas nueva a mas vieja.
- Usar github actions para compilar el cli, la ui
- Deberia dar algun soporte para MCP para poder conectar con las previews desde local
- voy a necesitar poder especificar en preview push db/files un db o dir files arbitrario y un .sql o .sql.hz o files.tgz o tar.gz
- Cuando dse crea el cache de la db se hace a la hora de crear el primer preview. Me pregunto si es posible sacar ese caso a un proceso en backgrround para no bloquear la generacion del preview. Y si no es posible por lo menos dar un poco mas de info "creando cache de esta db apras er usadaen las siguientes preview." ademas si se puede informar el progreso mejor que mejor.
- necesito integrar ia en lugares donde tenga sentido: "activa los proyectos que mas actividad tienen", "Si vas viendo previews que ninca se visitan despues de ser creadas en un proyeco en particular mejor no crees previews e informa al usuario que no se estan creando previews por falta de uso". Permite dejar un proment de lo que se espera a la hrtoa de hacer un erase automatico como por ejenmplo "en este proyecto por lo general revisamos las preview en un dia concreto aSI QUE NO TIENE SENTIDO QUE LAS CREES AUTOMATICAMENTE. MEJOR CREALAS CUANDO CREE UN mr A MASTER QUE SE LLAME "rELEASE XXX""""
- Quiero poder marcar como favoritos algunso preview de rama o mr. Cuando se borre el preview esta desaparece silencionamente.
- Visualizacion parent y sus hijos? por ejemplo todas las que apuntan a master se ven colgando de master, las de d11, colganod de d11, etc.
- Tengo storage box medio imlementado. es un poco mas lento pero mucho mas barato que r2. claude tiene memoria: por ahora el output sale mal codigicado y prev server se satura al procesar el output.
- el preview agent podria abrover cualquier cosa que suceda en la vm. por ejemplo el uso de cpu ram y disco que veo en la preview detail page.
-  "Usar estadísticas semanales de imágenes Docker más usadas para regenerar automáticamente el snapshot de VM con las imágenes con mayor probabilidad de uso, reduciendo el tiempo de pull en deploys."
- Pre-cargar imágenes Docker en el snapshot de VM para eliminar el pull en la mayoría de deploys, manteniendo la phase 'Pulling Docker images' como fallback para imágenes no incluidas en el snapshot."
- Necesito telemetria en los comandos de preview para saber si fallan y tener un output para poder arreglar.o. Evidntemente le tenemos que pedir permiso para recibitr estadisticas anonimas a los usuarios antes de hacerl. verdad?
- Cuando creo un preview y nop hay un pool listo va a crear una vm para dicho preview y si lo borro en pocoos segundos la vm va a quedar huervada. Parece que es porque el vm_id no se ha guardado todavia en la db cuando se elimina el preview pero deberia quedar segurado tan pronto como se le asigne la vm.
- los script de deploy tienen acceso a un toolkit TUI visual si se lo quisiera proporcionar desde fuera? me refiero a algun toolkit grafico para consolas que pueda simplemente usar porque   esta disponible en la vm o el docker (no se donde se ejecuta realmente.
- Vamos a cambiar la numeracion de los build. Ahora mismo se comparte entre todas las vm de todos los orgs. necesito que sea a nivel org o user o project.
- me gustaria poder hacer upload a las variables de entorno desde la ui para por ejemplo subir un json el lugar de copiarlo y pegarlo. Tiene sentido?


- Para guiar al usuario hay documentar como conectar gitlab a la app de previews:
   Ir a https://gitlab.com/oauth/applications
   Añadir aplicacion
   
   "User login"
   Callback: https://api.druploy.dev/api/gitlab/auth/callback
   Scopes: read_user
   
   Luego crear una nueva app para conectar la api:
   "Previews API"
   Callback: https://api.druploy.dev/api/gitlab/connect/callback
   Scopes: api

   "User login"
   Application ID: 3a4c9a8e1626f825734902c265eda47787b56f1724f09d65419e88991ab228d4
   Secret: gloas-85522b0b96f9a61bf169e10231a2422a6c59325dc324de1535cb0d4aecf8e0ee
   Callback: https://api.druploy.dev/api/gitlab/auth/callback
   Scopes: read_user
   
   "Previews API"
   Application ID: b05e4ef0609f8e02eec1bbe36770d1c847a155da9176b80bc568ba4b87dfe7e4
   Secret: gloas-4361304ca401bddf62cddac1cc37b3062b9c8e981fdada089765f44836d1acc6
   Callback: https://api.druploy.dev/api/gitlab/connect/callback
   Scopes: api


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



Si un preview se borra (por ejemplo por autoerase) necesitaria que la url en lugar de dar 404 lance un build inicial para reconstruir la imagen si el MR o la rama todavia existe. 
Idealmenete con un splash screen "La preview a la que quieres acceder actualmente no existe. Si la queres crear confirma este mensaje y en unos minutos la tendras disponible. 
No hace falta que cierres esta pestaña, tan pronto como acabe el build la pagina se va a mostrar"
Crees que es posible?