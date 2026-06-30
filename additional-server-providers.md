# Proveedores de servidores alternativos para Druploy

> Objetivo: ofrecer más de un proveedor para la creación de las VMs efímeras de previews.
> Análisis basado en cómo Druploy usa Hetzner hoy (`cloud.py`, `deployment.py`, `config/settings.py`).

---

## Requisitos que debe cumplir cualquier alternativa

Derivados del código actual (`cloud.py` es la única parte **no abstraída**; el almacenamiento ya tiene interfaz `StorageBackend`).

| Requisito | Dónde se usa | Por qué importa |
|---|---|---|
| **API/SDK de VMs** (crear, destruir, apagar, encender, listar, renombrar) | `create_vm`, `destroy_vm`, `shutdown_vm`, `power_on_vm`, `claim_pool_vm`, `get_active_vms` | Todo el ciclo de vida es programático |
| **Arranque desde snapshot/imagen propia** | `image=Image(id=settings.hetzner_snapshot_id)` | El *warm pool* y el arranque rápido dependen de una imagen pre-horneada |
| **Gestión de claves SSH por API** | `_ensure_ssh_key` (`client.ssh_keys.create`) | Se inyecta la clave pública al crear la VM |
| **Renombrar VMs en caliente** | `claim_pool_vm` usa `vm.update(name=...)` | El *warm pool* marca VMs como claimed renombrándolas |
| **Facturación por hora + precios vía API** | lee `price_hourly` / `price_monthly` para billing | Las VMs son efímeras → el modelo de coste es crítico |
| **IP pública IPv4 por VM** | `server.data_model.public_net.ipv4.ip` | Cada preview necesita su propia IP pública |
| **Aprovisionamiento rápido** | `wait_for_vm_ready`, warm pool (`create_pool_vm`, `claim_pool_vm`) | Hetzner arranca en segundos; un proveedor lento rompe el warm pool |
| **Instancias pequeñas y baratas** | tipo por defecto `cx23` (~2 vCPU, 4 GB RAM) | El atractivo de Hetuploy es el precio |
| **Datacenters en Europa** | — | Requisito de soberanía/latencia |

---

## Proveedores europeos

### Tier 1 — Cumplen TODOS los requisitos (API nativa completa)

#### Hetzner Cloud (actual) 🇩🇪

| Campo | Valor |
|---|---|
| País | 🇩🇪 Alemania |
| Zonas EU | Falkenstein (fsn1), Helsinki (hel1) |
| Snapshots custom | ✅ |
| SSH keys vía API | ✅ |
| Renombrar VM | ✅ |
| Precios vía API | ✅ (`price_hourly`, `price_monthly` en server types) |
| SDK Python | `hcloud` oficial |
| Tipo usado | cx23 (2 vCPU, 4 GB RAM) ≈ 4-5 €/mes |
| Observaciones | El que ya se usa. API madura, SDK excelente, precios bajos. |

#### Scaleway 🇫🇷 — *candidato nº 1*

| Campo | Valor |
|---|---|
| País | 🇫🇷 Francia |
| Zonas EU | París (fr-par-1/2/3), Ámsterdam (nl-ams-1/2/3), Varsovia (pl-waw-1/2/3) |
| Snapshots custom | ✅ (`POST /instance/v1/zones/{zone}/snapshots`, imágenes custom) |
| SSH keys vía API | ✅ |
| Renombrar VM | ✅ (`PATCH /instance/v1/zones/{zone}/servers/{id}`) |
| Precios vía API | ✅ (en instance types, facturación por hora/minuto) |
| SDK Python | `scaleway-sdk-python` oficial |
| Object Storage S3 | ✅ |
| Observaciones | La alternativa más directa a Hetzner. API REST completa y bien documentada. Snapshots, imágenes, security groups, flexible IPs. Mismo modelo de VMs efímeras. |

#### Exoscale 🇨🇭

| Campo | Valor |
|---|---|
| País | 🇨🇭 Suiza (subsidiaria de A1, telecom austríaca) |
| Zonas EU | Ginebra (ch-gva-2), Frankfurt (de-fra-1), Sofía (bg-sof-1) |
| Snapshots custom | ✅ (templates y snapshots) |
| SSH keys vía API | ✅ |
| Renombrar VM | ✅ (`PUT /v2/instance/{id}`) |
| Precios vía API | ✅ |
| SDK Python | `exoscale` oficial |
| Object Storage S3 | ✅ (SOS) |
| Observaciones | Filosofía muy similar a Hetzner: API REST limpia, modelos simples. Templates custom para imágenes con Docker. Subsidiaria de A1 (telecom austríaca). |

### Tier 2 — Cumplen API de VMs pero con matices

#### UpCloud 🇫🇮

| Campo | Valor |
|---|---|
| País | 🇫🇮 Finlandia |
| Zonas EU | Helsinki, Frankfurt, Madrid |
| Snapshots custom | ✅ (custom templates) |
| SSH keys vía API | ⚠️ Se inyectan vía server config al crear, no hay gestión independiente de keypairs |
| Renombrar VM | ❌ No nativo — habría que recrear o gestionar el rename a nivel de DB |
| Precios vía API | ❌ Precios en web, no en API |
| SDK Python | `upcloud-python-api` oficial |
| Object Storage S3 | ✅ |
| Observaciones | Buena API y rendimiento, arranque muy rápido (su punto fuerte → encaja bien con el warm pool). El rename del warm pool habría que rediseñarlo. El billing automático no funcionaría tal como está. |

#### IONOS Cloud 🇩🇪

| Campo | Valor |
|---|---|
| País | 🇩🇪 Alemania |
| Zonas EU | Alemania, España, Francia, Reino Unido |
| Snapshots custom | ✅ |
| SSH keys vía API | ✅ (SSH Key Manager dedicado) |
| Renombrar VM | ✅ |
| Precios vía API | ⚠️ Información limitada |
| SDK Python | SDK Python oficial |
| Object Storage S3 | ✅ |
| Observaciones | API completa pero orientada a enterprise. Modelo de Virtual Data Center (VDC) más complejo que el de Hetzner. Mayor curva de aprendizaje. Bien si prima soberanía DE. |

#### OVHcloud 🇫🇷

| Campo | Valor |
|---|---|
| País | 🇫🇷 Francia |
| Zonas EU | Francia (Gravelines, Roubaix, Strasbourg), Alemania (Frankfurt), Polonia (Varsovia) |
| Snapshots custom | ✅ (custom images vía OpenStack Glance) |
| SSH keys vía API | ✅ (OpenStack Nova keypairs) |
| Renombrar VM | ⚠️ No hay rename directo — hay que gestionar el nombre a nivel de DB/metadata |
| Precios vía API | ❌ Precios en web, no en API |
| SDK Python | `openstacksdk` o `ovh` SDK propio |
| Object Storage S3 | ✅ |
| Observaciones | Basado en OpenStack. 43 datacenters, el proveedor europeo más grande. Muy barato a escala. El aprovisionamiento suele ser algo más lento que Hetzner/UpCloud. El rename no es nativo → cambiar la lógica del warm pool. |

#### Open Telekom Cloud 🇩🇪

| Campo | Valor |
|---|---|
| País | 🇩🇪 Alemania (Deutsche Telekom) |
| Zonas EU | Frankfurt, Múnich |
| Snapshots custom | ✅ (OpenStack Glance images) |
| SSH keys vía API | ✅ (Nova keypairs) |
| Renombrar VM | ✅ (update server name) |
| Precios vía API | ❌ Precios en web, no en API |
| SDK Python | `openstacksdk` |
| Object Storage S3 | ✅ (Swift) |
| Observaciones | OpenStack puro operado por Deutsche Telekom. Buena soberanía de datos, orientado a empresa y sector público. Sin precios en API para billing automático. |

#### Infomaniak Public Cloud 🇨🇭

| Campo | Valor |
|---|---|
| País | 🇨🇭 Suiza |
| Zonas EU | Suiza (tier 3+ datacenters) |
| Snapshots custom | ✅ (custom images vía OpenStack) |
| SSH keys vía API | ✅ (OpenStack keypairs) |
| Renombrar VM | ✅ (update server) |
| Precios vía API | ❌ Precios en web, no en API |
| SDK Python | `openstacksdk` |
| Object Storage S3 | ✅ |
| Observaciones | OpenStack-based. Enfoque en sostenibilidad (energía renovable). Una sola zona (Suiza), lo que limita la redundancia. |

#### Leaseweb 🇳🇱

| Campo | Valor |
|---|---|
| País | 🇳🇱 Países Bajos |
| Zonas EU | Ámsterdam, Frankfurt |
| Snapshots custom | ✅ |
| SSH keys vía API | ⚠️ Gestión limitada vía API |
| Renombrar VM | ⚠️ No confirmado |
| Precios vía API | ❌ |
| SDK Python | REST API propia (sin SDK Python oficial, hay SDK PHP) |
| Observaciones | API documentada pero menos madura para el patrón de VMs efímeras con warm pool. Sin SDK Python oficial. |

### Resumen comparativo EU

| Proveedor | Snapshots | SSH keys | Rename VM | Precios API | SDK Python | Arranque | Tier |
|---|---|---|---|---|---|---|---|
| Hetzner (actual) | ✅ | ✅ | ✅ | ✅ | ✅ | Rápido | 1 |
| Scaleway | ✅ | ✅ | ✅ | ✅ | ✅ | Rápido | 1 |
| Exoscale | ✅ | ✅ | ✅ | ✅ | ✅ | Rápido | 1 |
| UpCloud | ✅ | ⚠️ | ❌ | ❌ | ✅ | Muy rápido | 2 |
| IONOS Cloud | ✅ | ✅ | ✅ | ⚠️ | ✅ | Medio | 2 |
| OVHcloud | ✅ | ✅ | ⚠️ | ❌ | ✅ | Medio | 2 |
| Open Telekom Cloud | ✅ | ✅ | ✅ | ❌ | ✅ | Medio | 2 |
| Infomaniak | ✅ | ✅ | ✅ | ❌ | ✅ | Medio | 2 |
| Leaseweb | ✅ | ⚠️ | ⚠️ | ❌ | ❌ | Medio | 2 |

---

## Proveedores no europeos (hyperscalers con datacenters en Europa)

> AWS, Google Cloud y Azure **técnicamente cumplen todos los requisitos** de Druploy, pero van en contra de la propuesta de valor (previews efímeras baratas y dentro de la UE).

### Encaje técnico (todos cumplen)

| Requisito Druploy | AWS EC2 | Azure VM | GCP Compute Engine |
|---|---|---|---|
| API + SDK Python de VMs | ✅ `boto3` | ✅ `azure-sdk` | ✅ `google-cloud-compute` |
| Imagen/snapshot propia | ✅ (AMIs) | ✅ (Managed Image) | ✅ (Custom Image) |
| Claves SSH por API | ✅ | ✅ | ✅ |
| Renombrar VM | ✅ (tags) | ✅ | ✅ |
| Billing por hora/segundo | ✅ por segundo | ✅ por segundo | ✅ por segundo |
| Precios vía API | ✅ (Pricing API) | ✅ (Retail Rates API) | ✅ (Cloud Billing Catalog API) |
| IP pública por VM | ✅ (Elastic IP) | ✅ (Public IP) | ✅ |
| Storage S3/objeto | ✅ S3 | 🟡 Blob (no S3 nativo) | ✅ GCS (modo S3 interop) |
| Aprovisionamiento rápido | ✅ | 🟡 algo más lento | ✅ |
| Zonas EU | Frankfurt, París, Irlanda | Frankfurt, Ámsterdam, París | Frankfurt, Ámsterdam, París |
| País de origen | 🇺🇸 EE.UU. | 🇺🇸 EE.UU. | 🇺🇸 EE.UU. |
| Soberanía de datos | ❌ CLOUD Act/FISA | ❌ CLOUD Act/FISA | ❌ CLOUD Act/FISA |

### Los problemas reales

1. **Coste (el principal).** VMs efímeras pequeñas:
   - Hetzner `cx23` (~2 vCPU / 4 GB) ≈ **4-5 €/mes**.
   - Equivalente en AWS (t3.small/t4g) / GCP / Azure ≈ **15-25 €/mes** on-demand → **3-5× más caro**.
   - Encima cobran **egress** (tráfico de salida), notable en un sistema de previews con mucho HTTP. Hetzner lo incluye casi gratis.
   - El modelo warm-pool + crear/destruir constante es justo donde los hyperscalers salen caros.

2. **Soberanía UE.** Son **empresas estadounidenses**. Aunque uses regiones en la UE, siguen sujetas a la **CLOUD Act** de EE. UU. → no es "dentro de la UE" en sentido jurídico estricto. Existen ofertas soberanas (*AWS European Sovereign Cloud*, Google con T-Systems, Azure con partners locales), pero **añaden coste y complejidad**.

3. **Complejidad operativa.** IAM, VPCs, security groups, etc. Levantar una VM simple requiere mucho más andamiaje que el `client.servers.create(...)` de Hetzner.

### Veredicto

- **¿Funcionarían?** Sí, sin problema técnico.
- **¿Como alternativa por defecto a Hetzner?** No: más caros y con la duda de soberanía.
- **¿Cuándo tienen sentido?** Como **backend opcional para clientes que lo pidan** (empresas que ya viven en AWS/GCP/Azure y quieren las previews en *su* cuenta), o por features exclusivas de un hyperscaler.

---

## Proveedores estilo Hetzner en EE. UU.

> Baratos, API simple y *developer-friendly* — los equivalentes americanos a Hetzner. Mucho más económicos que los hyperscalers y con APIs que se parecen 1-a-1 a la de Hetzner (crear/destruir/snapshot/clave SSH), por lo que son los que **menos esfuerzo de migración** requieren tras el refactor a `CloudManager`.

### Tier USA 1 — Cumplen todos los requisitos

#### DigitalOcean 🇺🇸 — *el referente*

| Campo | Valor |
|---|---|
| País | 🇺🇸 EE.UU. |
| Zonas USA | Nueva York, San Francisco (también EU: Frankfurt, Ámsterdam, Londres) |
| Snapshots custom | ✅ (Droplet Actions `snapshot` + custom images via `/v2/images`) |
| SSH keys vía API | ✅ (`/v2/account/keys` — CRUD completo) |
| Renombrar VM | ✅ (Droplet Action `type: "rename"`) |
| Precios vía API | ✅ (`GET /v2/sizes` — `price_monthly`, `price_hourly`) |
| SDK Python | `pydo` oficial |
| Object Storage S3 | ✅ (Spaces) |
| Observaciones | El "Hetzner americano" por excelencia. API casi *drop-in* en lugar de `hcloud`. Droplets desde ~4 $/mes. Arranque rápido, warm pool viable. Búsqueda por nombre es client-side (no hay endpoint nativo), pero filtrar por tag sí es server-side. |

#### Vultr 🇺🇸

| Campo | Valor |
|---|---|
| País | 🇺🇸 EE.UU. |
| Zonas USA | Atlanta, Chicago, Dallas, Los Ángeles, Miami, Nueva York, Seattle, Silicon Valley |
| Snapshots custom | ✅ (`/v2/snapshots` + custom ISOs) |
| SSH keys vía API | ✅ (`/v2/ssh-keys` — CRUD completo) |
| Renombrar VM | ✅ (`PATCH /v2/instances/{id}` — `label`) |
| Precios vía API | ✅ (`GET /v2/plans` — `price_monthly`, `price_hourly`) |
| SDK Python | `vultr` oficial |
| Object Storage S3 | ✅ |
| Observaciones | Mayor número de zonas USA (+32 datacenters globales). API REST limpia y simple. Cloud Compute desde ~4 $/mes. Búsqueda por nombre es client-side. |

#### Linode / Akamai Cloud 🇺🇸

| Campo | Valor |
|---|---|
| País | 🇺🇸 EE.UU. (Akamai Technologies) |
| Zonas USA | Atlanta, Dallas, Fremont, Newark, Washington DC |
| Snapshots custom | ✅ (`/images` — capture, list, get, delete, upload, replicate) |
| SSH keys vía API | ✅ (`/profile/sshkeys` — list/get/create/delete) |
| Renombrar VM | ✅ (`PUT /linode/instances/{id}` — `label`) |
| Precios vía API | ✅ (`GET /linode/types` — `price.hourly`, `price.monthly`) |
| SDK Python | `linode_api4` oficial |
| Object Storage S3 | ✅ |
| Observaciones | El que mejor cumple todos los requisitos. Soporta filtrado **server-side** por nombre (`X-Filter` header). API v4 muy madura. Renombrado nativo. Precios completos vía API. Nanode desde ~5 $/mes. |

#### Amazon Lightsail 🇺🇸 — *AWS simplificado*

| Campo | Valor |
|---|---|
| País | 🇺🇸 EE.UU. |
| Zonas USA | Todas las de AWS |
| Snapshots custom | ✅ |
| SSH keys vía API | ✅ |
| Renombrar VM | ✅ |
| Precios vía API | ⚠️ Pricing plano (no por hora en API) |
| SDK Python | `boto3` |
| Object Storage S3 | ✅ (AWS S3) |
| Observaciones | La versión "fácil y de precio plano" de AWS: VPS desde ~5 $/mes. Útil como puente hacia el ecosistema AWS sin la complejidad de EC2. |

### Tier USA 2 — Cumplen con matices

#### Kamatera 🇮🇱

| Campo | Valor |
|---|---|
| País | 🇮🇱 Israel (datacenters globales) |
| Zonas USA | Nueva York |
| Snapshots custom | ⚠️ `ServerImage` (create-from-server, delete), workflow más limitado |
| SSH keys vía API | ⚠️ Se inyecta la clave al crear la VM; no hay biblioteca independiente |
| Renombrar VM | ✅ (`Server.rename`) |
| Precios vía API | ⚠️ Existe comando `Quote` pero no catálogo completo |
| SDK Python | ❌ Solo REST API |
| Observaciones | Cloud VPS global, facturación por hora, DCs en varios continentes. Más débil en SSH keys, sin SDK Python, y pricing limitado en API. |

### Resumen comparativo USA

| Proveedor | Snapshots | SSH keys | Rename VM | Precios API | SDK Python | Búsqueda por nombre | Arranque | Tier |
|---|---|---|---|---|---|---|---|---|
| DigitalOcean | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ (client-side) | Rápido | USA 1 |
| Vultr | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ (client-side) | Rápido | USA 1 |
| Linode/Akamai | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (server-side) | Rápido | USA 1 |
| Lightsail | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | Medio | USA 1 |
| Kamatera | ⚠️ | ⚠️ | ✅ | ⚠️ | ❌ | ⚠️ (client-side) | Rápido | USA 2 |

**Mejor opción USA:** **Linode/Akamai** es el que más se acerca a Hetzner — cumple los requisitos sin matices, incluyendo filtrado server-side por nombre y renombrado nativo. **DigitalOcean** y **Vultr** son equivalentes muy sólidos con APIs casi *drop-in*.

---

## Otros proveedores globales compatibles

> "Compatible con Druploy" = **API/SDK de VMs + arranque desde imagen propia + almacenamiento de objetos**. Cualquier proveedor con esas 3 cosas encaja tras el refactor a `CloudManager`.

### Tier Global 1 — Cumplen todos los requisitos

#### Oracle Cloud Infrastructure (OCI) 🇺🇸

| Campo | Valor |
|---|---|
| País | 🇺🇸 EE.UU. (Oracle) |
| Zonas globales | USA, Europa, Asia, Oriente Medio, Brasil, Australia |
| Snapshots custom | ✅ (custom images — bring your own image) |
| SSH keys vía API | ✅ (key pairs en instance metadata + API de gestión) |
| Renombrar VM | ✅ (update instance — display name) |
| Precios vía API | ✅ (OCI Pricing API) |
| SDK Python | `oci` oficial |
| Object Storage S3 | ✅ |
| Observaciones | Plataforma cloud completa. **Free tier muy generoso** (Always Free, instancias ARM Ampere gratis). Flexible shapes (configurable vCPU/RAM). API madura. |

#### IBM Cloud (VPC) 🇺🇸

| Campo | Valor |
|---|---|
| País | 🇺🇸 EE.UU. (IBM) |
| Zonas globales | USA, Europa, Asia-Pacífico, América Latina |
| Snapshots custom | ✅ (custom images via VPC API) |
| SSH keys vía API | ✅ (`/vpcs/{vpc_id}/keys` — CRUD completo) |
| Renombrar VM | ✅ (update instance name) |
| Precios vía API | ✅ (IBM Cloud catalog API + pricing API) |
| SDK Python | `ibm-vpc` oficial |
| Object Storage S3 | ✅ |
| Observaciones | VPC compute moderno. API REST bien documentada. Catálogo amplio de servicios cloud. |

### Tier Global 2 — Cumplen con matices

#### Alibaba Cloud (ECS) 🇨🇳

| Campo | Valor |
|---|---|
| País | 🇨🇳 China |
| Zonas globales | China, USA, Europa (Frankfurt, Londres), Asia-Pacífico, Oriente Medio |
| Snapshots custom | ✅ (`CreateImage`, snapshots de disco) |
| SSH keys vía API | ✅ (`ImportKeyPair`, `DescribeKeyPairs`, `DeleteKeyPairs`) |
| Renombrar VM | ⚠️ No hay rename directo — se gestiona vía tags y `ModifyInstanceAttribute` |
| Precios vía API | ✅ (pricing API + `DescribePrice`) |
| SDK Python | `alibabacloud-python-sdk` oficial |
| Object Storage S3 | ✅ (OSS) |
| Observaciones | Mayor proveedor cloud de Asia. API extremadamente completa. El rename no es nativo (warm pool habría que rediseñarlo). Datacenters en Europa (Frankfurt, Londres). |

#### Tencent Cloud (CVM) 🇨🇳

| Campo | Valor |
|---|---|
| País | 🇨🇳 China |
| Zonas globales | China, USA, Europa (Frankfurt, Moscú), Asia-Pacífico |
| Snapshots custom | ✅ (`CreateImage`, snapshots) |
| SSH keys vía API | ✅ (`CreateKeyPair`, `DescribeKeyPairs`, `DeleteKeyPair`) |
| Renombrar VM | ⚠️ `ModifyInstanceAttribute` no cambia el nombre, se gestiona vía tags |
| Precios vía API | ✅ (pricing API + `InquirePrice`) |
| SDK Python | `tencentcloud-sdk-python` oficial |
| Object Storage S3 | ✅ (COS) |
| Observaciones | Segundo mayor proveedor cloud de China. API completa y madura. Mismo problema que Alibaba con el rename. Datacenters en Europa (Frankfurt). |

#### Huawei Cloud (ECS) 🇨🇳

| Campo | Valor |
|---|---|
| País | 🇨🇳 China |
| Zonas globales | China, Europa (París, Dublín), Asia-Pacífico, África |
| Snapshots custom | ✅ (`CreateImage`, snapshots de disco) |
| SSH keys vía API | ✅ (`ImportKeypair`, `ListKeypairs`, `DeleteKeypair`) |
| Renombrar VM | ⚠️ `UpdateServer` cambia metadata pero no el nombre del recurso |
| Precios vía API | ✅ (pricing API) |
| SDK Python | `huaweicloud-sdk-python` oficial |
| Object Storage S3 | ✅ |
| Observaciones | Proveedor cloud chino en expansión internacional. API basada en OpenStack internamente. Mismo patrón que Alibaba/Tencent con el rename. |

### Otros proveedores compatibles

| Proveedor | País | Nota |
|---|---|---|
| **Civo** 🇬🇧 | Reino Unido | Muy rápido, API limpia (orientado a K8s pero con compute). |
| **CloudSigma** 🇨🇭 | Suiza | IaaS flexible con API. |
| **Latitude.sh** 🌎 | Brasil/global | Bare metal con API (si se quiere metal en vez de VM). |
| **Contabo** 🇩🇪 | Alemania | Baratísimo, pero **API/snapshots más limitados** → ajuste más flojo para el warm pool. |
| **Naver Cloud** 🇰🇷 | Corea del Sur | Líder cloud en Corea. Sin SDK Python oficial. Documentación en inglés limitada. |
| **Rackspace** 🇺🇸 | EE.UU. | OpenStack. Sin precios en la compute API. Orientado a managed services. |

### A evitar

- **Yandex Cloud** 🇷🇺 — técnicamente compatible, pero **sanciones/geopolítica** lo descartan en la práctica.
- **PaaS** (Fly.io, Render, Railway, Vercel) — modelo distinto (contenedores/microVMs, no VMs gestionadas por ti) → **no encajan** con el diseño actual de Druploy.

### Resumen comparativo global

| Proveedor | País | Snapshots | SSH keys | Rename VM | Precios API | SDK Python | Tier |
|---|---|---|---|---|---|---|---|
| Oracle Cloud (OCI) | 🇺🇸 US | ✅ | ✅ | ✅ | ✅ | ✅ | Global 1 |
| IBM Cloud (VPC) | 🇺🇸 US | ✅ | ✅ | ✅ | ✅ | ✅ | Global 1 |
| Alibaba Cloud (ECS) | 🇨🇳 CN | ✅ | ✅ | ⚠️ | ✅ | ✅ | Global 2 |
| Tencent Cloud (CVM) | 🇨🇳 CN | ✅ | ✅ | ⚠️ | ✅ | ✅ | Global 2 |
| Huawei Cloud (ECS) | 🇨🇳 CN | ✅ | ✅ | ⚠️ | ✅ | ✅ | Global 2 |
| Naver Cloud | 🇰🇷 KR | ✅ | ✅ | ⚠️ | ⚠️ | ❌ | Global 2 |

---

## Plataformas europeas estilo AWS

Más allá de proveedores de VMs como Hetzner, existen plataformas europeas que ofrecen un catálogo de servicios comparable a AWS (compute, storage, managed databases, serverless, Kubernetes, CDN, etc.) bajo jurisdicción europea.

| Proveedor | País | Servicios | Observaciones |
|---|---|---|---|
| **Scaleway** | 🇫🇷 FR | Compute, Object Storage, Block Storage, Managed DB (Pg/MySQL/Redis/Mongo), Serverless (Functions/Containers/Jobs/SQL DB), Kubernetes, Container Registry, IoT Hub, Load Balancer, WAF, CDN, DNS, VPC, Secret Manager, Cockpit, Quantum | La plataforma europea más completa estilo AWS. API unificada. |
| **OVHcloud** | 🇫🇷 FR | Compute (OpenStack), Object Storage, Block Storage, Managed DB (MySQL/Pg/Redis/Kafka), Managed Kubernetes, Private Cloud, CDN, DNS, VPC | El proveedor europeo más grande. 43 datacenters. API fragmentada entre servicios. |
| **Open Telekom Cloud** | 🇩🇪 DE | Compute (OpenStack), Object Storage, Block Storage, Managed Databases (RDS), Kubernetes (CCE), CDN, DNS, VPC, IAM, KMS | Operado por Deutsche Telekom. OpenStack puro. Orientado a enterprise y sector público. |
| **Exoscale** | 🇨🇭 CH | Compute, Object Storage, Block Storage, Managed DB (Pg/MySQL/Kafka/Redis/Grafana/OpenSearch/Valkey), Kubernetes (SKS), DNS, IAM | Catálogo creciente pero más limitado. Enfoque en simplicidad. |
| **Infomaniak Public Cloud** | 🇨🇭 CH | Compute (OpenStack), Object Storage, Block Storage, Managed DB, Load Balancer, VPC | Plataforma más pequeña. Enfoque en sostenibilidad. |

### Comparación con AWS

| Servicio | AWS | Scaleway | OVHcloud | Open Telekom Cloud |
|---|---|---|---|---|
| Compute (VMs) | EC2 | Instances | Public Cloud | ECS |
| Object Storage | S3 | Object Storage | Object Storage | S3 (Swift) |
| Managed Kubernetes | EKS | Kapsule/Kosmos | Managed Kubernetes | CCE |
| Managed Databases | RDS | Managed DB | Managed DB | RDS |
| Serverless | Lambda | Functions/Containers/Jobs | ❌ | ❌ |
| Serverless SQL DB | Aurora | Serverless SQL DB | ❌ | ❌ |
| CDN | CloudFront | CDN | CDN | CDN |
| IAM | IAM | IAM | IAM | IAM |
| Container Registry | ECR | Container Registry | ❌ | SWR |
| Secret Manager | Secrets Manager | Secret Manager | ❌ | KMS |
| IoT | IoT Core | IoT Hub | ❌ | ❌ |
| Quantum | Braket | Quantum Computing | ❌ | ❌ |

**Scaleway** es el proveedor europeo que más se acerca a AWS en amplitud de catálogo.

---

## Almacenamiento — DB y files

Druploy usa dos backends de almacenamiento para los base files (DB dumps `.sql.gz` y archivos `.tar.gz`) y DB cache, abstractados tras la interfaz `StorageBackend` (`storage_backend.py`):

1. **S3-compatible Object Storage** (`storage.py`) — actualmente Cloudflare R2. Usa `boto3` con API S3 completa.
2. **Hetzner Storage Box** (`storage_box.py`) — SFTP vía `asyncssh`. NAS-style, sin presigned URLs.

> **La parte fácil:** a diferencia de las VMs, el almacenamiento **ya está abstraído**. Añadir alternativas es **trivial** (solo configuración) siempre que el proveedor hable **S3** o **SFTP**.

### Requisitos del backend S3 (ObjectStorageManager)

| Operación | Métodos S3 usados |
|---|---|
| Upload/download files | `upload_file`, `download_file` |
| Streaming | `get_object` (stream body) |
| Metadata (tamaño, fecha) | `head_object` |
| Presigned URLs (upload directo desde browser) | `generate_presigned_url` (PUT) |
| Multipart upload (archivos >5 GB) | `create_multipart_upload`, `upload_part`, `complete_multipart_upload`, `abort_multipart_upload` |
| Listar con prefijo (DB cache) | `list_objects_v2` |
| Borrado batch | `delete_objects` |
| Actualizar metadata (copy-in-place) | `copy_object` |
| Comandos en VM | `aws s3 cp` con `--endpoint-url` |

### Requisitos del backend SFTP (StorageBoxManager)

| Operación | Métodos SFTP/asyncssh usados |
|---|---|
| Upload/download files | `sftp.put`, `sftp.get` |
| Streaming | `sftp.open` + `read` en chunks |
| Metadata (tamaño, fecha) | `sftp.stat` |
| Listar directorio (DB cache) | `sftp.listdir` |
| Crear directorios | `sftp.mkdir` (recursive) |
| Borrar archivos | `sftp.remove` |
| Comandos en VM | `scp` con clave SSH |

### S3-compatible Object Storage — Europa

| Proveedor | País | Egress | Presigned URLs | Multipart | Observaciones |
|---|---|---|---|---|---|
| **Cloudflare R2** (actual) | 🇺🇸 US (data EU) | Gratis | ✅ | ✅ | El que ya se usa. Sin egress. S3 API completa. |
| **Scaleway Object Storage** | 🇫🇷 FR | Gratis (hasta 75GB/mes) | ✅ | ✅ | S3 completo. Multi-AZ. París, Ámsterdam, Varsovia. |
| **Exoscale SOS** | 🇨🇭 CH | Gratis | ✅ | ✅ | S3 completo. Ginebra, Frankfurt, Sofía. |
| **Hetzner Object Storage** | 🇩🇪 DE | Gratis | ✅ | ✅ | S3 completo. Falkenstein, Helsinki. |
| **OVHcloud Object Storage** | 🇫🇷 FR | Gratis (entrante) | ✅ | ✅ | S3 via Swift. Gravelines, Roubaix, Strasbourg, Frankfurt. |
| **UpCloud Object Storage** | 🇫🇮 FI | De pago | ✅ | ✅ | S3 completo. Helsinki, Frankfurt. |
| **IONOS Object Storage** | 🇩🇪 DE | De pago | ✅ | ✅ | S3 completo. Frankfurt, Berlín, Madrid. |
| **Leaseweb Object Storage** | 🇳🇱 NL | De pago | ✅ | ✅ | S3 completo. Ámsterdam, Frankfurt. |
| **Open Telekom Cloud** | 🇩🇪 DE | De pago | ✅ | ✅ | S3 via Swift. Frankfurt, Múnich. |
| **Infomaniak Object Storage** | 🇨🇭 CH | De pago | ✅ | ✅ | S3 completo. Suiza. |
| **Leafcloud** | 🇳🇱 NL | Gratis | ✅ | ✅ | Ceph-backed. Países Bajos. Energía renovable. |
| **Impossible Cloud** | 🇩🇪 DE | Gratis | ✅ | ✅ | Enterprise-grade. Enfoque soberanía. |
| **Cubbit DS3** | 🇮🇹 IT | Gratis | ✅ | ✅ | S3 compatible. Multi-region distribuido. 100% datos UE. |
| **Contabo Object Storage** | 🇩🇪 DE | Gratis | ✅ | ⚠️ | S3 básico. Limitaciones en multipart. |
| **Bunny Storage** | 🇸🇮 SI | Gratis | ⚠️ | ⚠️ | CDN-integrated. S3 API limitada. |

### S3-compatible Object Storage — USA y global

| Proveedor | País | Egress | Presigned URLs | Multipart | Observaciones |
|---|---|---|---|---|---|
| **AWS S3** | 🇺🇸 US | De pago | ✅ | ✅ | El estándar. Máxima compatibilidad. |
| **Backblaze B2** | 🇺🇸 US | Gratis (con CF) | ✅ | ✅ | Muy barato ($0.006/GB). Sin egress con Cloudflare. |
| **Wasabi** | 🇺🇸 US | Gratis (fair use) | ✅ | ✅ | Sin egress. ⚠️ Retención mínima de 90 días. |
| **DigitalOcean Spaces** | 🇺🇸 US | Gratis (hasta 1TB/mes) | ✅ | ✅ | ~5 $/mes. Zonas USA y Europa. |
| **Vultr Object Storage** | 🇺🇸 US | Gratis | ✅ | ✅ | Zonas USA, Europa, Asia. |
| **Linode Object Storage** | 🇺🇸 US | Gratis (hasta 1TB/mes) | ✅ | ✅ | Zonas USA y Europa. |
| **IDrive e2** | 🇺🇸 US | Gratis | ✅ | ✅ | Muy barato ($0.004/GB). |
| **Oracle Cloud Object Storage** | 🇺🇸 US | Gratis (10TB/mes) | ✅ | ✅ | Free tier. Zonas globales. |
| **IBM Cloud Object Storage** | 🇺🇸 US | De pago | ✅ | ✅ | Zonas globales. |
| **Alibaba Cloud OSS** | 🇨🇳 CN | De pago | ✅ | ✅ | Zonas globales. |
| **Tencent Cloud COS** | 🇨🇳 CN | Gratis (hasta 10GB/mes) | ✅ | ✅ | Zonas globales. |
| **MinIO** (self-hosted) | N/A | N/A | ✅ | ✅ | S3 100% compatible. Se hostea en cualquier VPS. Máximo control. |

### NAS / SFTP Storage — alternativas a Hetzner Storage Box

| Proveedor | País | Protocolo | SFTP | Precio aprox. | Observaciones |
|---|---|---|---|---|---|
| **Hetzner Storage Box** (actual) | 🇩🇪 DE | SFTP/SMB/SCP | ✅ | €3.59/mes (1TB) | El que ya se usa. SFTP, SMB, WebDAV, BorgBackup. |
| **rsync.net** | 🇺🇸 US | SFTP/ZFS | ✅ | ~€4/mes (1TB) | El estándar de oro en SFTP/ZFS gestionado. |
| **Time4VPS** | 🇱🇹 LT | SFTP/FTP | ✅ | ~€3.50/mes (2TB) | Lituania. Storage VPS con SFTP. Muy barato. |
| **BorgBase** | 🇩🇪 DE | SSH/Borg | ✅ | ~€4/mes (100GB) | Orientado a backup, accesible por SFTP. |
| **Contabo Storage VPS** | 🇩🇪 DE | SFTP | ✅ | ~€6/mes (400GB) | VPS con disco grande + SFTP propio, baratísimo. |
| **OVHcloud NAS** | 🇫🇷 FR | NFS/SMB | ❌ (NFS, no SFTP) | ~€25/mes (1.2TB) | NAS managed. No SFTP nativo → requeriría adaptar el backend. |
| **Infomaniak kDrive / Swiss Backup** | 🇨🇭 CH | SFTP/WebDAV | ✅ | Variable | Soberanía suiza. |
| **Storage VPS genérico** | Cualquiera | SFTP | ✅ | Variable | Cualquier VPS con disco grande + SSH. Requiere configurar SFTP manualmente. |
| **MinIO self-hosted** | N/A | S3 API | N/A | Coste del VPS | Alternativa a NAS: hostear MinIO en un VPS barato. S3 completo. |

### Recomendación de almacenamiento

**Para S3-compatible (sin refactor):** cualquier proveedor con S3 API completa funciona con `ObjectStorageManager` sin cambiar código — solo cambiando `HETZNER_S3_ENDPOINT`, `HETZNER_S3_ACCESS_KEY`, `HETZNER_S3_SECRET_KEY`, `HETZNER_S3_BUCKET`.

| Proveedor | Precio/GB | Egress | Soberanía EU | Compatible sin refactor |
|---|---|---|---|---|
| Cloudflare R2 (actual) | $0.015 | Gratis | ❌ (US) | ✅ |
| Backblaze B2 | $0.006 | Gratis (con CF) | ❌ (US) | ✅ |
| Scaleway | $0.01 | Gratis (75GB/mes) | ✅ | ✅ |
| Hetzner Object Storage | ~$0.01 | Gratis | ✅ | ✅ |
| Exoscale | ~$0.02 | Gratis | ✅ (CH) | ✅ |
| Wasabi | $0.0099 | Gratis | ❌ (US) | ✅ (⚠️ retención 90 días) |

**Para NAS/SFTP (sin refactor):** cualquier proveedor con acceso SFTP funciona con `StorageBoxManager` sin cambiar código — solo cambiando `STORAGEBOX_HOST`, `STORAGEBOX_PORT`, `STORAGEBOX_USER`, `STORAGEBOX_PASSWORD`, `STORAGEBOX_BASE_PATH`.

| Proveedor | Precio | SFTP nativo | Compatible sin refactor |
|---|---|---|---|
| Hetzner Storage Box (actual) | €3.59/mes (1TB) | ✅ | ✅ |
| rsync.net | ~€4/mes (1TB) | ✅ | ✅ |
| Time4VPS | ~€3.50/mes (2TB) | ✅ | ✅ |

> **Nota sobre refactor:** si se quiere soportar NAS con NFS (como OVHcloud NAS) o almacenamiento no-SFTP, habría que implementar un nuevo backend `NFSStorageManager` que herede de `StorageBackend`. La interfaz abstracta ya está preparada para esto.

---

## Impacto en el código

**Almacenamiento (fácil):** ya existe la interfaz `StorageBackend` con backends `s3` / `storagebox`. Casi todos los proveedores exponen S3 → reutilizable directamente sin tocar código.

**Cloud (trabajo real):** `cloud.py` está acoplado 100 % a `hcloud`. Para multi-proveedor haría falta:

1. Extraer una interfaz `CloudManager` con los mismos métodos:
   `create_vm`, `destroy_vm`, `shutdown_vm`, `power_on_vm`, `get_vm`,
   `wait_for_vm_ready`, `get_active_vms`, `get_pool_vms`, `create_pool_vm`,
   `claim_pool_vm`, `is_pool_vm`.
2. Una implementación por proveedor (Scaleway SDK; `openstacksdk` para OVH/OTC/Infomaniak; etc.).
3. Normalizar dos puntos delicados:
   - **Lectura de precios**: cada API los expone distinto → quizá una tabla de precios local por tipo de instancia.
   - **ID de snapshot/imagen**: cada proveedor tiene su propio formato → un campo de configuración por proveedor.

Los proveedores del Tier 2 requieren además cambios específicos:
- **Rename del warm pool**: UpCloud y OVHcloud no soportan rename en caliente → rediseñar el warm pool para marcar VMs como claimed en DB en lugar de renombrarlas.
- **Billing sin precios en API**: la mayoría de Tier 2 no exponen precios vía API → hardcoded o tabla de precios en config.
- **OpenStack SDK**: OVHcloud, Open Telekom Cloud e Infomaniak usan OpenStack → cambiar de `hcloud` a `openstacksdk` y adaptar el modelo de objetos.

---

## Resumen global

| Proveedor | País | API/SDK | Snapshots | SSH keys | Rename VM | Precios API | Billing/h | Storage S3 | Arranque | Tier |
|---|---|---|---|---|---|---|---|---|---|---|
| **Hetzner** (actual) | 🇩🇪 DE | ✅ `hcloud` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Rápido | EU 1 |
| **Scaleway** | 🇫🇷 FR | ✅ Python | ✅ | ✅ | ✅ | ✅ | ✅ (h/min) | ✅ | Rápido | EU 1 |
| **Exoscale** | 🇨🇭 CH | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (SOS) | Rápido | EU 1 |
| **UpCloud** | 🇫🇮 FI | ✅ Python | ✅ | ⚠️ | ❌ | ❌ | ✅ | ✅ | Muy rápido | EU 2 |
| **IONOS** | 🇩🇪 DE | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | Medio | EU 2 |
| **OVHcloud** | 🇫🇷 FR | ✅ OpenStack | ✅ | ✅ | ⚠️ | ❌ | ✅ | ✅ | Medio | EU 2 |
| **OTC** | 🇩🇪 DE | ✅ OpenStack | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | Medio | EU 2 |
| **Infomaniak** | 🇨🇭 CH | ✅ OpenStack | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | Medio | EU 2 |
| **AWS** | 🇺🇸 US (DCs UE) | ✅ `boto3` | ✅ AMI | ✅ | ✅ | ✅ | ✅ /seg | ✅ S3 | Rápido | — |
| **GCP** | 🇺🇸 US (DCs UE) | ✅ Python | ✅ | ✅ | ✅ | ✅ | ✅ /seg | ✅ GCS | Rápido | — |
| **Azure** | 🇺🇸 US (DCs UE) | ✅ Python | ✅ | ✅ | ✅ | ✅ | ✅ /seg | 🟡 Blob | Medio | — |
| **DigitalOcean** | 🇺🇸 US (DCs UE) | ✅ `pydo` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ Spaces | Rápido | USA 1 |
| **Vultr** | 🇺🇸 US (DCs UE) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Rápido | USA 1 |
| **Linode/Akamai** | 🇺🇸 US (DCs UE) | ✅ `linode_api4` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Rápido | USA 1 |
| **Lightsail** | 🇺🇸 US (DCs UE) | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ (AWS) | Medio | USA 1 |
| **Oracle Cloud** | 🇺🇸 US (DCs UE) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Medio | Global 1 |
| **IBM Cloud** | 🇺🇸 US (DCs UE) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Medio | Global 1 |
| **Alibaba Cloud** | 🇨🇳 CN (DCs UE) | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ✅ OSS | Rápido | Global 2 |
| **Tencent Cloud** | 🇨🇳 CN (DCs UE) | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ✅ COS | Rápido | Global 2 |
| **Huawei Cloud** | 🇨🇳 CN (DCs UE) | ✅ OpenStack | ✅ | ✅ | ⚠️ | ✅ | ✅ | ✅ | Medio | Global 2 |
| **Kamatera** | 🇮🇱 global | ✅ | ⚠️ | ⚠️ | ✅ | ⚠️ | ✅ | ✅ | Rápido | USA 2 |
| **Contabo** | 🇩🇪 DE | 🟡 limitada | 🟡 | — | — | ❌ | ✅ | ✅ | Medio | — |

### Mapa de candidatos por región

| Región | Recomendados | Resto compatible |
|---|---|---|
| **UE** | Scaleway, Exoscale | UpCloud, OVHcloud, IONOS, OTC, Infomaniak, Leaseweb |
| **EE. UU.** | DigitalOcean, Vultr, Linode | Lightsail, Kamatera, AWS/GCP/Azure |
| **Resto mundo** | Oracle Cloud (free tier), IBM Cloud | Alibaba, Huawei, Tencent, Civo, CloudSigma |

---

## Recomendación

Para reemplazar a Hetzner con **mínimo esfuerzo de refactor**, los únicos candidatos viables son **Scaleway** y **Exoscale** (Tier 1). Ambos ofrecen una API nativa completa con todas las operaciones que Druploy necesita: snapshots custom, SSH keys vía API, renombrado de VMs, y precios accesibles vía API para el sistema de billing.

**Recomendación práctica:** 2-3 backends (`CloudManager` implementations) cubren ~95 % de los casos sin disparar el coste de mantenimiento:
- **Hetzner** (actual) — Europa, barato.
- **Scaleway** — Europa, mejor ajuste global.
- **DigitalOcean** o **Linode** — USA/global, API casi *drop-in*.

Cada proveedor adicional = una implementación de `CloudManager` que mantener. A partir de aquí no hay un "siguiente gran proveedor" que falte; lo que importa ya no es *qué* proveedor, sino *cuántos* soportar de verdad.

> Nota: los detalles concretos (precios exactos, tiempos de arranque, estado GA de cada SDK) conviene verificarlos con datos actuales antes de elegir.
