Servidor preview server: 65.108.243.53

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
- Habra que poder limitar el acceso en base al dominio (por empresa)
- Voy a necesitar alguna sanitizacion para las db subidas o de eso se hace carlo el desarrolladdor?
- Cambiar el almacenamiento de configuraciones de /var/www/preview-manager/app-config.json a DB.
- La url https://api.preview-mr.com/ no deberia informar sobre lso endpoint. De hecho habria que revisar los endpoint y asegurarse que no queda nada expuesto que pueda ser peligroso.
- necesito extraer los tokens de clousflare, gitlab runner y el certificado ssh.
- Poder especificar un proceso de despliegue personalizado por rama! (Super útil cuando estás creando algo nuevo que requiera una configuración específica). Probablemente necesite ser especificado desde la conf de la rama ya que no se puede andar coniteando cambios para hacer pruebas.
- Acceso por consola via ui y posibilidad de configurar pu key para acceder a los ddev? (creo que no porque se puede acceder sin mas)
- posibilidad de definir variables de entorno exclusivas para el preview. va a haber veces que se este trabajando en una modificacion especifica que necesite sobreescribir o crear nuevas env vars.
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