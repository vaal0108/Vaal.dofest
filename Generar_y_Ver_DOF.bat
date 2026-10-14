@echo off
echo ====================================================
echo      ACTUALIZANDO DASHBOARD DOF ESTUDIANTES
echo ====================================================
echo.
echo Escaneando y clasificando publicaciones jurídicas...
echo Esto puede tomar un par de minutos. Por favor no cierres esta ventana.
echo.

cd /d "%~dp0"
python dof-estudiantes.py

echo.
echo ====================================================
echo      PROCESO FINALIZADO. ABRIENDO EL SITIO WEB...
echo ====================================================
start "" "Resumenes_Semanales\index.html"
exit
