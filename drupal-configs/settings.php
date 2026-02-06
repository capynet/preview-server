<?php

 // Load preview settings only on the preview server
 if (getenv('IS_PREVIEW_SERVER') === 'true') {
   if (file_exists($app_root . '/' . $site_path . '/settings.preview.php')) {
     include $app_root . '/' . $site_path . '/settings.preview.php';
   }
 }
