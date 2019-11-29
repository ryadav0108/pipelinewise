# MSSQL Integration

## Table of Contents
- [Introduction](#introduction)
- [How-To](#how-to)
- [FAQ](#faq)
- [Troubleshooting](#troubleshooting)

## Introduction
## How-To
To integrate the [singer-io/tap-mssql](https://github.com/singer-io/tap-mssql) with PipelineWise, following files files have to be modified
* [tap.json](pipelinewise/cli/schemas/tap.json) Add tap-mssql in the list of taps
  ```
  "type": {
      "type": "string",
      "enum": [
        "tap-postgres",
        "tap-mssql",
        "tap-mysql",
        "tap-oracle",
        "tap-adwords",
        "tap-zendesk",
        "tap-kafka",
        "tap-s3-csv",
        "tap-snowflake",
        "tap-salesforce",
        "tap-jira"
      ]
    }
  ```
* [tap_properties.py](pipelinewise/cli/tap_properties.py) Add tap-mssql configuration to be returned by `get_tap_properties`
   ```
  'tap-mssql': {
        'tap_config_extras': {
            # Generate unique server id's to avoid broken connection
            # when multiple taps reading from the same mysql server
            # 'server_id': generate_tap_mysql_server_id()
        },
        'tap_stream_id_pattern': '{{database_name}}_{{schema_name}}_{{table_name}}',
        'tap_stream_name_pattern': '{{database_name}}_{{schema_name}}_{{table_name}}',
        'tap_catalog_argument': '--catalog',
        'default_replication_method': 'FULL_TABLE',
        'default_data_flattening_max_level': 0
    }
   ```
* Files to be added or modified for installation of tap-mssql using [install.sh](install.sh)
    * Add folder names **tap-mssql** in [singer-connectors](singer-connectors) containing text file named **git-clone.txt** containing url for the repository
      ```
      https://github.com/singer-io/tap-mssql.git
      ```
    * [install.sh](install.sh) Add method to clone the [singer-io/tap-mssql](https://github.com/singer-io/tap-mssql) into .virtualenv directory during installation
      ```
      clone_connector() {
        echo
        echo "--------------------------------------------------------------------------"
        echo "Cloning $1 connector..."
        echo "--------------------------------------------------------------------------"
        cd $SRC_DIR/singer-connectors/$1
        URL=$(head -n 1 git-clone.txt)
        cd $VENV_DIR
        git clone $URL
       }
      ```
    * call method during the installation of taps and targets
      ```
      # Install Singer connectors
      install_connector tap-adwords
      install_connector tap-jira
      install_connector tap-kafka
      clone_connector tap-mssql
      install_connector tap-mysql
      install_connector tap-postgres
      install_connector tap-s3-csv
      install_connector tap-salesforce
      install_connector tap-snowflake
      install_connector tap-zendesk
      install_connector target-s3-csv
      install_connector target-snowflake
      install_connector transform-field
      install_connector tap-oracle
      install_connector target-postgres
      install_connector target-redshift
      ```

## FAQ
**Q**. Why is tap-mssql clone not installed?

**A**. tap-mssql is written in clojure whereas other tap and targets are written in python with existing modules available for download via pip. Due to this the latest code from the repository is pulled

**Q**. Is PipelineWise flow affected due to tap-mssql not written in python and unavailability of it's own virtual environment?

**A**. The execution flow of PipelineWise isn't affected by tap-mssql being written in clojure as it follows the same project activation by a shell script used by other singer/transferwise tap and targets.

## Troubleshooting
1.  No table is selected when running import command.
    *   **dbname** should be replaced with **database**, as tap-mssql requires database keyword for mapping of selected tables. Using dbname, makes the selected file to be 'None', making the selected tables to be not included

2.  Connection error during the import state for tap-mssql.
    *   tap-mssql requires keywords **database**, **host**, **password**, **port** and **user** in configuration json used for connecting to the source database. If any of the configuration is missing connection error or login error may arise. To ensure the particular keywords are sent to tap-mssql by PipelineWise, YAML file for the tap-mssql should have the mentioned keywords.
    
        NOTE:
        Changing keyword **dbname** to **database** requires modification to PipelineWise as well, as PipelineWise looks for dbname in the configuration. [config.py](pipelinewise/cli/config.py) Has to updated to make it also accept database in configuration. Update method _save_tap_jsons_
        ```
        tap_dbname = tap_config.get('dbname')
        ```
        Update the above mentioned code with following code
        ```
        tap_dbname = tap_config.get('dbname') if tap_config.get('dbname') is not None else tap_config.get('database')
        ```

3.  Unsupported Primary key or data data type error during replication stage of PipelineWise
    *   The tap-mssql currently does not support User defined and few built-in data types(eg. 'xml', 'geography', etc). Due to this they are saved as unsupported after in the properties file created after the import stage. When replication is initiated, If a table with unsupported data type is selected, It is unable to parse the value to any specific data type. 
    
        To solve this the tap-mssql has to updated to make it get the data types of User defined objects from which they are derived from and the currently unsupported MSSQL data types such as 'xml' and 'geography' has to modified to use string parsing.
        
        Modify the [catalog.clj](.virtualenvs/tap-mssql/src/tap_mssql/catalog.clj) in the tap-mssql after installation make it replicate the unsupported data types.
        *   Update the method `add-unsupported?-data` with the following lines of code
            ```
            (defn add-unsupported?-data
              [config column]
              (if (nil? (column->schema column))
                (let [conn-map (assoc (config/->conn-map config)
                                 :dbname
                                 (get config "database"))
                      sql-query (str (format "SELECT C.DATA_TYPE From INFORMATION_SCHEMA.COLUMNS AS C WHERE C.COLUMN_NAME='%s' AND C.TABLE_NAME='%s' AND C.TABLE_SCHEMA='%s'"
                                             (column :column_name) (column :table_name) (column :table_schem)))
                      base-type (first (jdbc/query conn-map sql-query))
                      updated-column (assoc column :type_name (base-type :data_type))]
                  (if (nil? (column->schema updated-column))
                  (assoc column :type_name "nvarchar")
                  updated-column))
                column))
            ```
        *   Update the method `get-database-columns` with the following lines of code
            ```
            (defn get-database-columns
              [config database]
              (let [conn-map (assoc (config/->conn-map config)
                                    :dbname
                                    (:table_cat database))
                    raw-columns (get-database-raw-columns conn-map database)
                    ;; group by "schema_name.table_name" so we can look it up
                    row-count-data (->> (get-approximate-row-count conn-map)
                                        (group-by #(format "%s.%s" (:schema_name %) (:table_name %))))
                    ;; group by "database.schema.table" so we can look it up
                    primary-key-data (->> (get-primary-keys conn-map)
                                          (group-by #(format "%s.%s.%s" (:table_catalog %) (:table_schema %) (:table_name %))))]
                (->> raw-columns
                     (map (partial add-primary-key?-data primary-key-data))
                     (map (partial add-is-view?-data conn-map))
                     (map (partial add-row-count-data row-count-data))
                     (map (partial add-unsupported?-data config) ))))
            ```
        To apply the changes at the the time of installation of PipelineWise, make the specified changes
        *   Add the modified [catalog.clj](.virtualenvs/tap-mssql/src/tap_mssql/catalog.clj) in [tap-mssql](singer-connectors/tap-mssql)
        *   Update [install.sh](install.sh) to replace the existing file with the modified one
            *   Add method `apply_fix`
                ```
                apply_fix() {
                    cp $SRC_DIR/singer-connectors/$1/catalog.clj $VENV_DIR/$1/src/tap_mssql/
                }
                ```
            *   Add method call after cloning of tap-mssql
                ```
                # Install Singer connectors
                install_connector tap-adwords
                install_connector tap-jira
                install_connector tap-kafka
                clone_connector tap-mssql
                apply_fix tap-mssql
                install_connector tap-mysql
                install_connector tap-postgres
                install_connector tap-s3-csv
                install_connector tap-salesforce
                install_connector tap-snowflake
                install_connector tap-zendesk
                install_connector target-s3-csv
                install_connector target-snowflake
                install_connector transform-field
                install_connector tap-oracle
                install_connector target-postgres
                install_connector target-redshift
                ```
