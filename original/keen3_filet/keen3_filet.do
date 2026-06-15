# Template Do File For Altium Designer -> Specctra Autorouter
# Altium Limited
# 22-Apr-2015
#
unit mil
bestsave on C:\Users\johnp\Documents\ware\net2sch\keen3_filet.bst
status_file C:\Users\johnp\Documents\ware\net2sch\keen3_filet.sts
grid smart (wire 1) (via 1)
smart_route
critic

#enable the spread and miter features if you have the DFM option
#spread
#miter

# If you have the DFM module use spread and miter instead of the following. 
# Comment these lines out
Center
Recorner Diagonal 2000 2000 2000
Recorner Diagonal 1000 1000 1000
Recorner Diagonal 500 500 500
Recorner Diagonal 250 250 250
Recorner Diagonal 125 125 125
Recorner Diagonal 100 100 100
Recorner Diagonal 50 50 50
Recorner Diagonal 25 25 25
Recorner Diagonal 10 10 10
# Stop commenting here if you have the DFM module


write  routes      C:\Users\johnp\Documents\ware\net2sch\keen3_filet.rte
write  wires       C:\Users\johnp\Documents\ware\net2sch\keen3_filet.w
report conflicts   C:\Users\johnp\Documents\ware\net2sch\keen3_filet.rcf
report corners     C:\Users\johnp\Documents\ware\net2sch\keen3_filet.rcn
report rules       C:\Users\johnp\Documents\ware\net2sch\keen3_filet.rrl
report status      C:\Users\johnp\Documents\ware\net2sch\keen3_filet.rst
report unconnect   C:\Users\johnp\Documents\ware\net2sch\keen3_filet.ruc
report vias        C:\Users\johnp\Documents\ware\net2sch\keen3_filet.rva
quit
